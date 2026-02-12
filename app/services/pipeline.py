# app/services/pipeline.py
"""
Pipeline 编排器
职责：串联预处理层、分类层、决策层、执行层、反馈层
实现完整的 RAG 流水线
"""
import time
from datetime import datetime
from typing import List, Optional, Dict, Any, Iterator
from loguru import logger

from app.services.schemas import (
    PipelineInput,
    PipelineOutput,
    ClassificationResult,
    RouteDecision,
    HistoryMessage,
    LatencyMetrics
)
from app.services.query_rewriter import QueryRewriter, get_query_rewriter
from app.services.intent_classifier import IntentClassifier, get_intent_classifier
from app.services.local_llm_service import get_local_llm_service
from app.services.router import Router, get_router
from app.services.session_state import SessionStateManager, get_session_state_manager
from app.services.pipeline_logger import PipelineLogger, get_pipeline_logger
from app.services.pipeline_tracer import PipelineTracer
from app.services.vector_store import make_dedup_key
from app.utils.text_encoding import safe_for_display
from concurrent.futures import ThreadPoolExecutor


def _sanitize_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """对发往小程序的事件做安全显示过滤，避免 ◇ 等乱码。"""
    out = dict(event)
    if "choices" in out and out["choices"]:
        delta = (out["choices"][0].get("delta") or {}).copy()
        if "content" in delta and isinstance(delta["content"], str):
            delta["content"] = safe_for_display(delta["content"])
        out["choices"] = [{"delta": delta}]
    if "replace" in out and isinstance(out["replace"], str):
        out["replace"] = safe_for_display(out["replace"])
    if "sources" in out:
        out["sources"] = [
            {
                k: safe_for_display(v) if isinstance(v, str) else v
                for k, v in (src if isinstance(src, dict) else {}).items()
            }
            for src in out["sources"]
        ]
    return out


class Pipeline:
    """
    RAG Pipeline 编排器
    
    数据流：
    UserInput -> QueryRewriter -> IntentClassifier -> Router -> Executor -> Logger -> Response
    """
    
    def __init__(
        self,
        query_rewriter: QueryRewriter = None,
        intent_classifier: IntentClassifier = None,
        router: Router = None,
        state_manager: SessionStateManager = None,
        pipeline_logger: PipelineLogger = None,
        vector_store = None,
        llm_service = None
    ):
        """
        初始化 Pipeline
        
        Args:
            query_rewriter: 查询改写器
            intent_classifier: 意图分类器
            router: 路由决策器
            state_manager: 会话状态管理器
            pipeline_logger: 日志记录器
            vector_store: 向量存储（检索用）
            llm_service: LLM服务（生成用）
        """
        self._query_rewriter = query_rewriter
        self._intent_classifier = intent_classifier
        self._router = router
        self._state_manager = state_manager
        self._pipeline_logger = pipeline_logger
        self._vector_store = vector_store
        self._llm_service = llm_service
    
    # ========== 延迟初始化属性 ==========
    
    @property
    def query_rewriter(self) -> QueryRewriter:
        if self._query_rewriter is None:
            self._query_rewriter = get_query_rewriter()
        return self._query_rewriter
    
    @property
    def intent_classifier(self) -> IntentClassifier:
        if self._intent_classifier is None:
            self._intent_classifier = get_intent_classifier()
        return self._intent_classifier
    
    @property
    def router(self) -> Router:
        if self._router is None:
            self._router = get_router()
        return self._router
    
    @property
    def state_manager(self) -> SessionStateManager:
        if self._state_manager is None:
            self._state_manager = get_session_state_manager()
        return self._state_manager
    
    @property
    def pipeline_logger(self) -> PipelineLogger:
        if self._pipeline_logger is None:
            self._pipeline_logger = get_pipeline_logger()
        return self._pipeline_logger
    
    @property
    def vector_store(self):
        if self._vector_store is None:
            from app.services.vector_store import VectorStore
            self._vector_store = VectorStore()
        return self._vector_store
    
    @property
    def llm_service(self):
        if self._llm_service is None:
            from app.services.llm_service import LLMService
            self._llm_service = LLMService(
                local_llm_service=get_local_llm_service()
            )
        return self._llm_service
    
    # ========== 搜索辅助 ==========

    # 子查询覆盖阈值：最高检索分数 >= 此值则视为"已覆盖"
    _SUB_QUERY_COVERAGE_SCORE = 0.45

    # 单次混合检索时对命中目标分类的结果施加的软提权
    _CATEGORY_BOOST = 0.08

    # 改写置信度熔断：原句与改写句向量相似度低于此值时，双轨 RRF 中提高原始轨权重
    _REWRITE_CONFIDENCE_THRESHOLD = 0.75

    # time_sensitivity -> RRF 时间信号权重 alpha
    # alpha 越大，时间排名对最终排序的影响越大
    _TIME_RRF_ALPHA = {
        "realtime": 1.0,
        "recent": 0.5,
        "historical": 0.2,
        "none": 0.1,
    }

    @staticmethod
    def _apply_time_rerank(
        results: List[Dict[str, Any]],
        anchor_date: datetime,
        time_alpha: float = 0.1,
    ) -> List[Dict[str, Any]]:
        """
        使用 Reciprocal Rank Fusion (RRF) 融合语义排名和时间排名。

        不直接对向量相似度分数做乘法（避免破坏语义阈值），
        而是将语义排名和时间近邻排名作为两个独立信号，通过 RRF 融合。

        RRF: score = 1/(k + rank_sem) + alpha * 1/(k + rank_time)

        Args:
            results: 搜索结果列表（会被原地修改）
            anchor_date: 锚点日期
            time_alpha: 时间信号权重（越大越偏好新内容）
        Returns:
            按 RRF score 降序排列的结果列表
        """
        if not results:
            return results

        k = 60  # RRF 标准常数
        anchor_naive = anchor_date.replace(tzinfo=None)

        # 计算每条结果与锚点的时间距离
        for item in results:
            published_time = item.get("published_time")
            if not published_time:
                item["_days_diff"] = float("inf")
                continue
            try:
                pub_dt = datetime.fromisoformat(published_time)
                pub_naive = pub_dt.replace(tzinfo=None)
                item["_days_diff"] = abs((anchor_naive - pub_naive).total_seconds()) / 86400.0
            except Exception:
                item["_days_diff"] = float("inf")

        # 语义排名（按原始 score 降序，rank 从 0 开始）
        semantic_order = sorted(
            range(len(results)), key=lambda i: results[i].get("score", 0), reverse=True
        )
        rank_sem = {i: rank for rank, i in enumerate(semantic_order)}

        # 时间排名（按 days_diff 升序 = 越新排名越靠前）
        time_order = sorted(
            range(len(results)), key=lambda i: results[i].get("_days_diff", float("inf"))
        )
        rank_time = {i: rank for rank, i in enumerate(time_order)}

        # RRF 融合
        for idx, item in enumerate(results):
            sem_rrf = 1.0 / (k + rank_sem[idx])
            time_rrf = 1.0 / (k + rank_time[idx])
            item["original_score"] = item.get("score", 0)
            days = item.get("_days_diff", float("inf"))
            item["time_weight"] = round(
                1.0 / (1.0 + days / 30.0), 4
            ) if days != float("inf") else 0.0
            item["score"] = round(sem_rrf + time_alpha * time_rrf, 6)
            item.pop("_days_diff", None)

        results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return results

    def _resolve_time_rerank_params(
        self,
        search_params: Dict[str, Any],
        current_date_str: Optional[str],
    ) -> tuple:
        """
        从 search_params 中解析出 RRF 时间重排所需的 anchor_date 和 time_alpha。

        Returns:
            (anchor_date: datetime, time_alpha: float)
        """
        # 锚点日期：优先 reference_datetime，其次 current_date，最后 now()
        ref_dt_str = search_params.get("reference_datetime")
        anchor_date = None
        if ref_dt_str:
            try:
                anchor_date = datetime.strptime(ref_dt_str, "%Y-%m-%d")
            except ValueError:
                pass
        if anchor_date is None and current_date_str:
            try:
                anchor_date = datetime.strptime(current_date_str, "%Y-%m-%d")
            except ValueError:
                pass
        if anchor_date is None:
            anchor_date = datetime.now()

        time_sensitivity = search_params.get("time_sensitivity", "none")
        time_alpha = self._TIME_RRF_ALPHA.get(time_sensitivity, 0.1)

        return anchor_date, time_alpha

    def _search_hybrid(
        self,
        search_queries: List[str],
        standalone_query: str,
        top_k: int,
        filter_source: Optional[str],
        filter_category: Optional[str],
        filter_categories: Optional[List[str]] = None,
        filter_date_from: Optional[str] = None,
        filter_date_to: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        检索：优先在前三类别 filter_categories 中搜；无则退化为单类 + 软提权。
        """
        fallback_query = standalone_query.replace("最新", "").replace("动态", "").replace("情况", "").strip()
        use_top3 = bool(filter_categories)
        effective_k = top_k if use_top3 else (top_k * 3 if filter_category else top_k)

        results = self.vector_store.search_with_expansion(
            queries=search_queries,
            top_k=effective_k,
            filter_source=filter_source,
            filter_category=None if use_top3 else filter_category,
            filter_categories=filter_categories,
            filter_date_from=filter_date_from,
            filter_date_to=filter_date_to,
            fallback_query=fallback_query if fallback_query != standalone_query else None,
        )

        # 无 top3 时：category 软提权
        if not use_top3 and filter_category and results:
            for item in results:
                if item.get("category") == filter_category:
                    item["score"] = item.get("score", 0) + self._CATEGORY_BOOST
            results.sort(key=lambda x: x.get("score", 0), reverse=True)

        return results[:top_k]

    @staticmethod
    def _rrf_merge_two_lists(
        list_a: List[Dict[str, Any]],
        list_b: List[Dict[str, Any]],
        k: int = 60,
        weight_a: float = 1.0,
        weight_b: float = 1.0,
    ) -> List[Dict[str, Any]]:
        """
        将两条检索结果列表按 RRF (Reciprocal Rank Fusion) 融合。
        score = weight_a * 1/(k+rank_a) + weight_b * 1/(k+rank_b)，按 dedup_key 去重。
        """
        rank_a = {make_dedup_key(item): i for i, item in enumerate(list_a)}
        rank_b = {make_dedup_key(item): i for i, item in enumerate(list_b)}
        key_to_item: Dict[str, Dict[str, Any]] = {}
        for item in list_a:
            key_to_item[make_dedup_key(item)] = item
        for item in list_b:
            key = make_dedup_key(item)
            if key not in key_to_item:
                key_to_item[key] = item
        rrf_scores = []
        for key, item in key_to_item.items():
            rrf = 0.0
            if key in rank_a:
                rrf += weight_a * 1.0 / (k + rank_a[key])
            if key in rank_b:
                rrf += weight_b * 1.0 / (k + rank_b[key])
            rrf_scores.append((rrf, item))
        rrf_scores.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in rrf_scores]

    # ========== 查询分解 + 独立检索 ==========

    def _search_decomposed(
        self,
        standalone_query: str,
        top_k: int,
        filter_source: Optional[str],
        filter_category: Optional[str],
        filter_categories: Optional[List[str]] = None,
        filter_date_from: Optional[str] = None,
        filter_date_to: Optional[str] = None,
        original_query: Optional[str] = None,
    ) -> tuple:
        """
        带查询分解的搜索：自动判断是否需要拆分查询，每个子查询独立检索。
        单一意图且 original_query != standalone_query 时做双轨检索（原始 query 一轨 + 改写变体一轨）并 RRF 融合。

        Returns:
            (search_results, all_search_queries, covered_sub_queries, missed_sub_queries)
        """
        sub_queries = self.llm_service.decompose_query(standalone_query)

        if len(sub_queries) <= 1:
            # ---- 单一意图：改写变体检索 ----
            search_queries = self.llm_service.expand_queries_for_search(
                standalone_query, num_variants=3
            )
            logger.info(f"检索查询(扩展): {search_queries}")
            results_rewritten = self._search_hybrid(
                search_queries=search_queries,
                standalone_query=standalone_query,
                top_k=top_k,
                filter_source=filter_source,
                filter_category=filter_category,
                filter_categories=filter_categories,
                filter_date_from=filter_date_from,
                filter_date_to=filter_date_to,
            )
            results = results_rewritten
            # 双轨：原始 query 单次检索，与改写变体结果 RRF 融合
            if original_query and original_query.strip() != standalone_query.strip():
                weight_original, weight_rewritten = 1.0, 1.0
                try:
                    emb_orig = self.vector_store.embedding_service.encode_query(original_query)
                    emb_rewr = self.vector_store.embedding_service.encode_query(standalone_query)
                    cos_sim = sum(a * b for a, b in zip(emb_orig, emb_rewr))
                    if cos_sim < self._REWRITE_CONFIDENCE_THRESHOLD:
                        weight_original, weight_rewritten = 1.5, 1.0
                        logger.info(
                            f"改写置信度熔断: cos_sim={cos_sim:.4f} < {self._REWRITE_CONFIDENCE_THRESHOLD}, "
                            "提高原始轨 RRF 权重"
                        )
                except Exception as e:
                    logger.debug(f"改写置信度计算跳过: {e}")
                try:
                    results_original = self.vector_store.search(
                        query_text=original_query,
                        top_k=top_k,
                        filter_source=filter_source,
                        filter_category=filter_category if not filter_categories else None,
                        filter_categories=filter_categories,
                        filter_date_from=filter_date_from,
                        filter_date_to=filter_date_to,
                    )
                    results = self._rrf_merge_two_lists(
                        results_original, results_rewritten,
                        k=60, weight_a=weight_original, weight_b=weight_rewritten,
                    )[:top_k]
                    logger.info("双轨检索: 已用原始 query 一轨与改写变体 RRF 融合")
                except Exception as e:
                    logger.warning(f"双轨检索原始轨失败，仅用改写结果: {e}")
            # 单一意图不产生 coverage gap 与不对称提示
            return results, search_queries, [], [], ""

        # ---- 多意图：并发检索 ----
        logger.info(f"多意图查询分解: {sub_queries}")

        # Step 1: 生成各子查询的检索变体
        sub_variants: Dict[str, List[str]] = {}
        all_search_queries: List[str] = []
        for sub_q in sub_queries:
            variants = self.llm_service.expand_queries_for_search(sub_q, num_variants=2)
            sub_variants[sub_q] = variants
            all_search_queries.extend(variants)
            logger.info(f"  子查询 '{sub_q}' 检索变体: {variants}")

        # Step 2: 并发执行各子查询的向量检索
        sub_query_results: Dict[str, List[Dict]] = {}
        with ThreadPoolExecutor(max_workers=min(len(sub_queries), 4)) as executor:
            futures = {}
            for sub_q, variants in sub_variants.items():
                futures[sub_q] = executor.submit(
                    self._search_hybrid,
                    search_queries=variants,
                    standalone_query=sub_q,
                    top_k=top_k,
                    filter_source=filter_source,
                    filter_category=filter_category,
                    filter_categories=filter_categories,
                    filter_date_from=filter_date_from,
                    filter_date_to=filter_date_to,
                )
            for sub_q, future in futures.items():
                sub_query_results[sub_q] = future.result()
                logger.info(f"  子查询 '{sub_q}': 检索到 {len(sub_query_results[sub_q])} 条")

        # Step 3: 合并去重 + 记录每个子查询的最高分
        all_results: Dict[str, Dict] = {}
        sub_query_best_scores: Dict[str, float] = {}
        for sub_q in sub_queries:
            sub_results = sub_query_results.get(sub_q, [])
            best = 0.0
            for item in sub_results:
                key = make_dedup_key(item)
                score = item.get("score", 0)
                best = max(best, score)
                if key not in all_results or score > all_results[key].get("score", 0):
                    all_results[key] = item
            sub_query_best_scores[sub_q] = best

        # Step 4: 基于分数阈值判定覆盖（替代原有的独占率判定）
        # 只要子查询有高于阈值的检索结果，即视为已覆盖，
        # 不再因为结果与其他子查询重叠而误判为"未覆盖"
        covered: List[str] = []
        missed: List[str] = []
        for sub_q in sub_queries:
            best = sub_query_best_scores.get(sub_q, 0)
            if best >= self._SUB_QUERY_COVERAGE_SCORE:
                covered.append(sub_q)
                logger.info(
                    f"  子查询 '{sub_q}': 最高分 {best:.4f} >= "
                    f"{self._SUB_QUERY_COVERAGE_SCORE} -> 已覆盖"
                )
            else:
                missed.append(sub_q)
                logger.info(
                    f"  子查询 '{sub_q}': 最高分 {best:.4f} < "
                    f"{self._SUB_QUERY_COVERAGE_SCORE} -> 未覆盖"
                )

        merged = sorted(all_results.values(), key=lambda x: x.get("score", 0), reverse=True)
        # 子意图非对称提示：某子意图最高分明显低于其他时，提醒生成层勿脑补
        asymmetry_lines: List[str] = []
        max_best = max(sub_query_best_scores.values()) if sub_query_best_scores else 0
        if max_best > 0 and len(sub_queries) >= 2:
            for sub_q in sub_queries:
                best = sub_query_best_scores.get(sub_q, 0)
                if best < 0.5 * max_best:
                    asymmetry_lines.append(
                        f"[注意] 子意图「{sub_q}」的检索结果较少或相关性较低，"
                        "回答时请勿根据其他子意图脑补该部分的细节。"
                    )
        asymmetry_note = "\n".join(asymmetry_lines) if asymmetry_lines else ""
        return merged, all_search_queries, covered, missed, asymmetry_note

    @staticmethod
    def _build_coverage_note(covered: List[str], missed: List[str]) -> str:
        """
        构建检索覆盖情况说明。

        仅在多意图查询且存在未命中的子查询时生成，
        注入到 LLM 的 user prompt 中作为结构化事实信息，
        让 LLM 在播报时自然引述（而非自行做 meta-cognition 判断缺失）。
        """
        if not missed:
            return ""
        lines = ["[检索覆盖情况]"]
        for q in covered:
            lines.append(f"- {q}：已检索到相关新闻")
        for q in missed:
            lines.append(f"- {q}：未检索到相关内容")
        return "\n".join(lines)

    # ========== 核心方法 ==========
    
    def run(self, input_data: PipelineInput) -> PipelineOutput:
        """
        执行完整的 Pipeline 流程
        
        Args:
            input_data: Pipeline 输入
            
        Returns:
            PipelineOutput 输出
        """
        start_time = time.time()
        request_id = self.pipeline_logger.create_request_id()
        tracer = PipelineTracer(request_id)
        latency = LatencyMetrics()
        
        try:
            # 1. 获取对话历史
            history = self._load_conversation_history(
                conversation_id=input_data.conversation_id,
                max_turns=input_data.history_turns
            )
            tracer.record_input(
                raw_query=input_data.query,
                conversation_id=input_data.conversation_id,
                history=history,
            )
            
            # 2. 分类前置：先根据原始 query 定调（大分类），再在分类约束下改写
            t0 = time.time()
            current_date = input_data.current_date or datetime.now().strftime("%Y-%m-%d")
            classification = self.intent_classifier.classify(
                standalone_query=input_data.query,
                current_date=current_date,
                history=history,
                original_query=None,
            )
            latency.classify_ms = (time.time() - t0) * 1000
            if latency.classify_ms < 5:
                classify_prompt = f"(规则前置命中，未调用 LLM)\n查询: {input_data.query}"
            else:
                classify_prompt = self.intent_classifier.CLASSIFY_PROMPT.format(
                    current_date=current_date,
                    query=input_data.query,
                    category_options=self.intent_classifier.CATEGORY_OPTIONS,
                )
            tracer.record_classify(
                prompt=classify_prompt,
                result_dict=classification.model_dump(),
                elapsed_ms=latency.classify_ms,
            )

            # 3. 预处理层：查询改写（在分类约束下，两阶段：独立性判断 + LLM 改写）
            t0 = time.time()
            rewrite_result = self.query_rewriter.rewrite(
                current_input=input_data.query,
                history=history,
                category_hint=classification.filter_category,
            )
            latency.rewrite_ms = (time.time() - t0) * 1000
            standalone_query = rewrite_result.standalone_query
            rewrite_reasoning = rewrite_result.reasoning
            rewrite_skipped = (standalone_query.strip() == input_data.query.strip())
            rewrite_prompt = ""
            if not rewrite_skipped and history:
                history_text = self.query_rewriter._format_history(history)
                category_constraint = ""
                if classification.filter_category:
                    category_constraint = (
                        f"当前用户问题已被判定属于「{classification.filter_category}」领域，"
                        "改写时请勿偏离该领域，仅做指代消解与信息补全。\n\n"
                    )
                rewrite_prompt = self.query_rewriter.REWRITE_PROMPT.format(
                    history=history_text,
                    current_input=input_data.query,
                    category_constraint=category_constraint,
                )
            tracer.record_rewrite(
                prompt=rewrite_prompt,
                result=standalone_query,
                elapsed_ms=latency.rewrite_ms,
                skipped=rewrite_skipped,
                reasoning=rewrite_reasoning,
            )
            
            # 4. 决策层：路由决策
            t0 = time.time()
            state = self.state_manager.get_state(
                input_data.conversation_id or "anonymous"
            )
            route_decision = self.router.decide(
                classification=classification,
                state=state,
                standalone_query=standalone_query
            )
            latency.route_ms = (time.time() - t0) * 1000
            tracer.record_route(
                action=route_decision.action,
                reason=route_decision.reason,
                elapsed_ms=latency.route_ms,
            )
            
            # 5. 执行层：根据路由执行
            t0 = time.time()
            answer, sources, retrieval_count = self._execute(
                route_decision=route_decision,
                standalone_query=standalone_query,
                original_query=input_data.query,
                input_data=input_data,
                history=history,
                tracer=tracer,
                rewrite_reasoning=rewrite_reasoning,
            )
            latency.retrieve_ms = (time.time() - t0) * 1000 if route_decision.action == "search_then_generate" else 0
            latency.generate_ms = (time.time() - t0) * 1000 - latency.retrieve_ms
            
            # 6. 更新会话状态
            self.state_manager.update_state(
                conversation_id=input_data.conversation_id or "anonymous",
                classification=classification,
                standalone_query=standalone_query,
                route_action=route_decision.action
            )
            
            # 7. 记录日志
            latency.total_ms = (time.time() - start_time) * 1000
            self.pipeline_logger.log(
                request_id=request_id,
                conversation_id=input_data.conversation_id,
                raw_input=input_data.query,
                standalone_query=standalone_query,
                classification=classification,
                route_decision=route_decision,
                retrieval_count=retrieval_count,
                final_response=answer,
                latency=latency
            )
            
            # -- trace: 最终输出 --
            tracer.record_glm_output(answer=answer)
            tracer.flush(total_ms=latency.total_ms)
            
            return PipelineOutput(
                answer=answer,
                sources=sources,
                classification=classification,
                route_decision=route_decision,
                standalone_query=standalone_query,
                query_time=latency.total_ms / 1000
            )
            
        except Exception as e:
            latency.total_ms = (time.time() - start_time) * 1000
            logger.error(f"Pipeline 执行失败: {e}")
            tracer.record_error(str(e))
            tracer.flush(total_ms=latency.total_ms)
            self.pipeline_logger.log(
                request_id=request_id,
                conversation_id=input_data.conversation_id,
                raw_input=input_data.query,
                standalone_query=input_data.query,
                classification=ClassificationResult(
                    needs_search=True,
                    intent_type="news",
                    filter_category="general",
                    filter_categories=["general"],
                    time_sensitivity="none",
                    confidence=0.0
                ),
                route_decision=RouteDecision(action="fallback", reason=str(e)),
                retrieval_count=0,
                final_response="",
                latency=latency,
                error=str(e)
            )
            raise

    def run_stream(self, input_data: PipelineInput) -> Iterator[Dict[str, Any]]:
        """
        流式执行 Pipeline，yield SSE 事件 dict。
        事件格式：{"choices": [{"delta": {"content": "..."}}]} 或 {"replace": "..."} 或 {"sources": [...], "done": True}
        """
        start_time = time.time()
        request_id = self.pipeline_logger.create_request_id()
        tracer = PipelineTracer(request_id)
        latency = LatencyMetrics()
        accumulated_answer = ""
        classification = None
        route_decision = None
        standalone_query = None
        retrieval_count = 0
        final_sources = []
        try:
            history = self._load_conversation_history(
                conversation_id=input_data.conversation_id,
                max_turns=input_data.history_turns
            )
            # -- trace: 原始输入 --
            tracer.record_input(
                raw_query=input_data.query,
                conversation_id=input_data.conversation_id,
                history=history,
            )

            # ===== STEP 1: 分类前置（先根据原始 query 定调） =====
            t0 = time.time()
            current_date = input_data.current_date or datetime.now().strftime("%Y-%m-%d")
            classification = self.intent_classifier.classify(
                standalone_query=input_data.query,
                current_date=current_date,
                history=history,
                original_query=None,
            )
            latency.classify_ms = (time.time() - t0) * 1000
            if latency.classify_ms < 5:
                classify_prompt = f"(规则前置命中，未调用 LLM)\n查询: {input_data.query}"
            else:
                classify_prompt = self.intent_classifier.CLASSIFY_PROMPT.format(
                    current_date=current_date,
                    query=input_data.query,
                    category_options=self.intent_classifier.CATEGORY_OPTIONS,
                )
            tracer.record_classify(
                prompt=classify_prompt,
                result_dict=classification.model_dump(),
                elapsed_ms=latency.classify_ms,
            )

            # ===== STEP 2: 查询改写（在分类约束下） =====
            t0 = time.time()
            rewrite_result = self.query_rewriter.rewrite(
                current_input=input_data.query,
                history=history,
                category_hint=classification.filter_category,
            )
            latency.rewrite_ms = (time.time() - t0) * 1000
            standalone_query = rewrite_result.standalone_query
            rewrite_reasoning = rewrite_result.reasoning
            rewrite_skipped = (standalone_query.strip() == input_data.query.strip())
            rewrite_prompt = ""
            if not rewrite_skipped and history:
                history_text = self.query_rewriter._format_history(history)
                category_constraint = ""
                if classification.filter_category:
                    category_constraint = (
                        f"当前用户问题已被判定属于「{classification.filter_category}」领域，"
                        "改写时请勿偏离该领域，仅做指代消解与信息补全。\n\n"
                    )
                rewrite_prompt = self.query_rewriter.REWRITE_PROMPT.format(
                    history=history_text,
                    current_input=input_data.query,
                    category_constraint=category_constraint,
                )
            tracer.record_rewrite(
                prompt=rewrite_prompt,
                result=standalone_query,
                elapsed_ms=latency.rewrite_ms,
                skipped=rewrite_skipped,
                reasoning=rewrite_reasoning,
            )

            # ===== STEP 3: 路由决策 =====
            t0 = time.time()
            state = self.state_manager.get_state(
                input_data.conversation_id or "anonymous"
            )
            route_decision = self.router.decide(
                classification=classification,
                state=state,
                standalone_query=standalone_query
            )
            latency.route_ms = (time.time() - t0) * 1000
            # -- trace: 路由 --
            tracer.record_route(
                action=route_decision.action,
                reason=route_decision.reason,
                elapsed_ms=latency.route_ms,
            )

            # ===== STEP 4 & 5: 执行（搜索 + 生成 / 直接生成）=====
            t0 = time.time()
            raw_stream_answer = ""  # GLM 原始流输出（后处理前）
            for event in self._execute_stream(
                route_decision=route_decision,
                standalone_query=standalone_query,
                original_query=input_data.query,
                input_data=input_data,
                history=history,
                tracer=tracer,
                rewrite_reasoning=rewrite_reasoning,
            ):
                if "choices" in event and event["choices"]:
                    delta = event["choices"][0].get("delta") or {}
                    content = delta.get("content")
                    if isinstance(content, str):
                        accumulated_answer += content
                        raw_stream_answer += content
                if "replace" in event:
                    accumulated_answer = event["replace"]
                if event.get("done"):
                    final_sources = event.get("sources") or []
                    retrieval_count = len(final_sources)
                yield event
            # 完整回复打出到控制台，便于排查格式/乱码等问题
            if accumulated_answer:
                logger.info("agent 回复:\n{}", accumulated_answer)
            latency.retrieve_ms = (time.time() - t0) * 1000 if route_decision.action == "search_then_generate" else 0
            latency.generate_ms = (time.time() - t0) * 1000 - (latency.retrieve_ms if route_decision.action == "search_then_generate" else 0)
            latency.total_ms = (time.time() - start_time) * 1000
            self.state_manager.update_state(
                conversation_id=input_data.conversation_id or "anonymous",
                classification=classification,
                standalone_query=standalone_query,
                route_action=route_decision.action
            )
            self.pipeline_logger.log(
                request_id=request_id,
                conversation_id=input_data.conversation_id,
                raw_input=input_data.query,
                standalone_query=standalone_query,
                classification=classification,
                route_decision=route_decision,
                retrieval_count=retrieval_count,
                final_response=accumulated_answer,
                latency=latency
            )
            # -- trace: GLM 输出（raw_stream = 原始流，answer = 后处理后）--
            tracer.record_glm_output(
                answer=accumulated_answer,
                raw_stream=raw_stream_answer,
            )
            tracer.flush(total_ms=latency.total_ms)
        except Exception as e:
            latency.total_ms = (time.time() - start_time) * 1000
            logger.error(f"Pipeline 流式执行失败: {e}")
            tracer.record_error(str(e))
            tracer.flush(total_ms=latency.total_ms)
            self.pipeline_logger.log(
                request_id=request_id,
                conversation_id=input_data.conversation_id,
                raw_input=input_data.query,
                standalone_query=input_data.query or "",
                classification=classification or ClassificationResult(
                    needs_search=True, intent_type="news", filter_category="general",
                    filter_categories=["general"], time_sensitivity="none", confidence=0.0
                ),
                route_decision=route_decision or RouteDecision(action="fallback", reason=str(e)),
                retrieval_count=0,
                final_response="",
                latency=latency,
                error=str(e)
            )
            raise
    
    def _load_conversation_history(
        self,
        conversation_id: Optional[str],
        max_turns: int
    ) -> List[HistoryMessage]:
        """从数据库加载对话历史"""
        if not conversation_id:
            return []
        
        try:
            from app.models import ConversationMessage
            
            # 查询最近的消息
            messages = ConversationMessage.query.filter(
                ConversationMessage.conversation_id == conversation_id
            ).order_by(
                ConversationMessage.created_at.desc()
            ).limit(max_turns * 2).all()
            
            # 转换格式并反转顺序（从旧到新）
            history = []
            for msg in reversed(messages):
                role = "user" if msg.speaker == "user" else "assistant"
                history.append(HistoryMessage(
                    role=role,
                    content=msg.content,
                    timestamp=msg.created_at
                ))
            
            return history
            
        except Exception as e:
            logger.warning(f"加载对话历史失败: {e}")
            return []
    
    def _execute(
        self,
        route_decision: RouteDecision,
        standalone_query: str,
        original_query: str,
        input_data: PipelineInput,
        history: Optional[List[HistoryMessage]] = None,
        tracer: Optional[PipelineTracer] = None,
        rewrite_reasoning: Optional[str] = None,
    ) -> tuple[str, List[Dict[str, Any]], int]:
        """
        执行路由决策

        Returns:
            (answer, sources, retrieval_count)
        """
        if route_decision.action == "search_then_generate":
            return self._execute_search_then_generate(
                route_decision=route_decision,
                standalone_query=standalone_query,
                original_query=original_query,
                input_data=input_data,
                tracer=tracer,
                rewrite_reasoning=rewrite_reasoning,
            )
        
        elif route_decision.action == "generate_direct":
            return self._execute_generate_direct(
                original_query=original_query,
                history=history,
                tracer=tracer,
            )
        
        elif route_decision.action in ("tool_quote", "tool_weather"):
            # 工具未接入：明确拒绝，不做幻觉生成
            tool_name = route_decision.action.replace("tool_", "")
            refusal = f"抱歉，{tool_name}工具暂未接入，无法为你获取实时数据。"
            logger.warning(f"工具调用未实现，明确拒绝: {route_decision.action}")
            return refusal, [], 0
        
        else:
            # Fallback
            logger.warning(f"未知路由动作，执行直接生成: {route_decision.action}")
            return self._execute_generate_direct(
                original_query=original_query,
                history=history,
                tracer=tracer,
            )

    def _execute_stream(
        self,
        route_decision: RouteDecision,
        standalone_query: str,
        original_query: str,
        input_data: PipelineInput,
        history: Optional[List[HistoryMessage]] = None,
        tracer: Optional[PipelineTracer] = None,
        rewrite_reasoning: Optional[str] = None,
    ) -> Iterator[Dict[str, Any]]:
        """执行层流式版本，yield SSE 事件 dict（content / replace / done）。"""
        if route_decision.action == "search_then_generate":
            search_params = route_decision.search_params or {}
            filter_category = input_data.filter_category or search_params.get("filter_category")
            filter_categories = search_params.get("filter_categories")
            filter_source = input_data.filter_source or search_params.get("filter_source")
            filter_date_from = input_data.filter_date_from or search_params.get("filter_date_from")
            filter_date_to = input_data.filter_date_to or search_params.get("filter_date_to")
            # 查询分解 + 并发检索（多意图查询每个子查询拥有独立 top_k 配额）；category 为 top3 时在前三类别中搜
            search_results, search_queries, covered, missed, asymmetry_note = self._search_decomposed(
                standalone_query=standalone_query,
                top_k=input_data.top_k,
                filter_source=filter_source,
                filter_category=filter_category,
                filter_categories=filter_categories,
                filter_date_from=filter_date_from,
                filter_date_to=filter_date_to,
                original_query=original_query,
            )
            coverage_note = self._build_coverage_note(covered, missed)
            if asymmetry_note:
                coverage_note = (coverage_note + "\n" + asymmetry_note) if coverage_note else asymmetry_note
            if coverage_note:
                logger.info(f"检索覆盖情况:\n{coverage_note}")
            # RRF 时间重排
            anchor_date, time_alpha = self._resolve_time_rerank_params(
                search_params, input_data.current_date
            )
            search_results = self._apply_time_rerank(
                search_results, anchor_date, time_alpha
            )
            logger.info(
                f"RRF 时间重排: anchor={anchor_date.strftime('%Y-%m-%d')}, "
                f"time_alpha={time_alpha}"
            )
            logger.info(f"RAG 搜索结果(流式): 共 {len(search_results)} 条")
            for i, item in enumerate(search_results, 1):
                orig = item.get('original_score')
                tw = item.get('time_weight')
                extra = f" (sem={orig:.4f}, tw={tw:.4f})" if orig is not None else ""
                logger.info(
                    f"  [{i}] score={item.get('score', 0):.6f}{extra} | {item.get('published_time', '')} | {item.get('source', '')} | {item.get('title', '')[:60]}"
                )
            # -- trace: 搜索结果（含完整正文）--
            if tracer:
                tracer.record_search(
                    search_queries=search_queries,
                    results=search_results,
                    anchor_date=anchor_date,
                    time_alpha=time_alpha,
                )
            if not search_results:
                yield _sanitize_event({"choices": [{"delta": {"content": "抱歉，没有找到相关的新闻内容。"}}]})
                yield _sanitize_event({"sources": [], "done": True})
                return
            sources = [
                {
                    "title": item.get("title"),
                    "source": item.get("source"),
                    "category": item.get("category"),
                    "link": item.get("link"),
                    "score": item.get("score"),
                    "published_time": item.get("published_time")
                }
                for item in search_results
            ]
            # -- trace: 重建 GLM 完整 prompt（与 generate_answer_stream 内部一致）--
            # 使用 standalone_query（已融合历史上下文），避免歧义代词导致 LLM 无法理解
            if tracer:
                from app.services.llm_service import LLMService as _LLM
                _sys_prompt = _LLM._build_news_system_prompt()
                _ctx_text = "\n\n".join(
                    f"{i}. {_LLM._format_news_item(item)}"
                    for i, item in enumerate(search_results, 1)
                )
                _user_text = _LLM._build_news_user_prompt(
                    _ctx_text, standalone_query, coverage_note,
                    original_query=original_query,
                    rewrite_reasoning=rewrite_reasoning,
                )
                _full_user = f"{_user_text}\n\nRead again:\n{_user_text}"
                tracer.record_glm_prompt(
                    system_prompt=_sys_prompt,
                    user_prompt=_full_user,
                )
            deep_think = getattr(input_data, 'deep_think', False)
            for event in self.llm_service.generate_answer_stream(
                query=standalone_query,
                context=search_results,
                deep_think=deep_think,
                coverage_note=coverage_note,
                original_query=original_query,
                rewrite_reasoning=rewrite_reasoning,
            ):
                yield _sanitize_event(event)
            yield _sanitize_event({"sources": sources, "done": True})
        elif route_decision.action in ("tool_quote", "tool_weather"):
            # 工具未接入：明确拒绝，不做幻觉生成
            tool_name = route_decision.action.replace("tool_", "")
            refusal = f"抱歉，{tool_name}工具暂未接入，无法为你获取实时数据。"
            logger.warning(f"工具调用未实现，明确拒绝: {route_decision.action}")
            yield _sanitize_event({"choices": [{"delta": {"content": refusal}}]})
            yield _sanitize_event({"sources": [], "done": True})
        else:
            # generate_direct / unknown action -> 直接生成（带历史）
            deep_think = getattr(input_data, 'deep_think', False)
            messages = self._build_chat_messages(original_query, history)
            # -- trace: 直接生成路径的完整 messages --
            if tracer:
                tracer.record_glm_prompt(
                    system_prompt=messages[0].get("content", "") if messages else "",
                    user_prompt="\n---\n".join(
                        f"[{m.get('role', '?')}] {m.get('content', '')}"
                        for m in messages[1:]
                    ),
                )
            for chunk in self.llm_service.chat_stream(messages, deep_think=deep_think):
                yield _sanitize_event({"choices": [{"delta": {"content": chunk}}]})
            yield _sanitize_event({"sources": [], "done": True})
    
    def _execute_search_then_generate(
        self,
        route_decision: RouteDecision,
        standalone_query: str,
        original_query: str,
        input_data: PipelineInput,
        tracer: Optional[PipelineTracer] = None,
        rewrite_reasoning: Optional[str] = None,
    ) -> tuple[str, List[Dict[str, Any]], int]:
        """执行检索后生成"""
        # 合并检索参数
        search_params = route_decision.search_params or {}
        
        # 优先使用输入参数中的过滤条件
        filter_category = input_data.filter_category or search_params.get("filter_category")
        filter_categories = search_params.get("filter_categories")
        filter_source = input_data.filter_source or search_params.get("filter_source")
        filter_date_from = input_data.filter_date_from or search_params.get("filter_date_from")
        filter_date_to = input_data.filter_date_to or search_params.get("filter_date_to")
        
        # 1. 查询分解 + 并发检索（category 为 top3 时在前三类别中搜）
        search_results, search_queries, covered, missed, asymmetry_note = self._search_decomposed(
            standalone_query=standalone_query,
            top_k=input_data.top_k,
            filter_source=filter_source,
            filter_category=filter_category,
            filter_categories=filter_categories,
            filter_date_from=filter_date_from,
            filter_date_to=filter_date_to,
            original_query=original_query,
        )
        coverage_note = self._build_coverage_note(covered, missed)
        if asymmetry_note:
            coverage_note = (coverage_note + "\n" + asymmetry_note) if coverage_note else asymmetry_note
        if coverage_note:
            logger.info(f"检索覆盖情况:\n{coverage_note}")
        
        # RRF 时间重排
        anchor_date, time_alpha = self._resolve_time_rerank_params(
            search_params, input_data.current_date
        )
        search_results = self._apply_time_rerank(
            search_results, anchor_date, time_alpha
        )
        logger.info(
            f"RRF 时间重排: anchor={anchor_date.strftime('%Y-%m-%d')}, "
            f"time_alpha={time_alpha}"
        )
        
        # -- trace: 搜索结果 --
        if tracer:
            tracer.record_search(
                search_queries=search_queries,
                results=search_results,
                anchor_date=anchor_date,
                time_alpha=time_alpha,
            )
        
        if not search_results:
            return "抱歉，没有找到相关的新闻内容。", [], 0
        
        logger.info(f"RAG 搜索结果(同步): 共 {len(search_results)} 条")
        for i, item in enumerate(search_results, 1):
            orig = item.get('original_score')
            tw = item.get('time_weight')
            extra = f" (sem={orig:.4f}, tw={tw:.4f})" if orig is not None else ""
            logger.info(
                f"  [{i}] score={item.get('score', 0):.6f}{extra} | {item.get('published_time', '')} | {item.get('source', '')} | {item.get('title', '')[:60]}"
            )
        
        # -- trace: 重建 GLM 完整 prompt --
        # 使用 standalone_query（已融合历史上下文），避免歧义代词导致 LLM 无法理解
        if tracer:
            from app.services.llm_service import LLMService as _LLM
            _sys_prompt = _LLM._build_news_system_prompt()
            _ctx_text = "\n\n".join(
                f"{i}. {_LLM._format_news_item(item)}"
                for i, item in enumerate(search_results, 1)
            )
            _user_text = _LLM._build_news_user_prompt(
                _ctx_text, standalone_query, coverage_note,
                original_query=original_query,
                rewrite_reasoning=rewrite_reasoning,
            )
            _full_user = f"{_user_text}\n\nRead again:\n{_user_text}"
            tracer.record_glm_prompt(
                system_prompt=_sys_prompt,
                user_prompt=_full_user,
            )
        
        # 3. LLM 生成回答（使用 standalone_query；Grounding 中传入 original_query 与 rewrite_reasoning）
        answer = self.llm_service.generate_answer(
            query=standalone_query,
            context=search_results,
            coverage_note=coverage_note,
            original_query=original_query,
            rewrite_reasoning=rewrite_reasoning,
        )
        
        # 4. 格式化来源
        sources = [
            {
                "title": item.get("title"),
                "source": item.get("source"),
                "category": item.get("category"),
                "link": item.get("link"),
                "score": item.get("score"),
                "published_time": item.get("published_time")
            }
            for item in search_results
        ]
        
        return answer, sources, len(search_results)
    
    def _execute_generate_direct(
        self,
        original_query: str,
        history: Optional[List[HistoryMessage]] = None,
        tracer: Optional[PipelineTracer] = None,
    ) -> tuple[str, List[Dict[str, Any]], int]:
        """执行直接生成（不检索），带对话历史以支持多轮"""
        messages = self._build_chat_messages(original_query, history)
        # -- trace: 直接生成路径 --
        if tracer:
            tracer.record_glm_prompt(
                system_prompt=messages[0].get("content", "") if messages else "",
                user_prompt="\n---\n".join(
                    f"[{m.get('role', '?')}] {m.get('content', '')}"
                    for m in messages[1:]
                ),
            )
        answer = self.llm_service.chat(messages)
        return answer, [], 0

    @staticmethod
    def _build_chat_messages(
        current_query: str,
        history: Optional[List[HistoryMessage]] = None
    ) -> List[Dict[str, str]]:
        """
        将对话历史 + 当前查询组装为 LLM messages 列表。
        格式: [system, ...history(user/assistant), user(当前)]
        """
        SYSTEM_PROMPT = (
            "你叫菠萝包，是一个亲切、自然、像老朋友一样的 AI 助手。"
            "减少说\"哈哈\"\"看来\"\"无论如何\"\"随时为你服务\"等废话的使用频率。"
            "你具备极强的洞察力，能从用户随性、口语化甚至破碎的表达中，精准捕捉其真实意图。"
            "请用中文简洁准确地回答。\n\n"
            "当用户问价格/重量换算（如美元每盎司换算成人民币每克）时，按下面示例的方式推理并回答。\n\n"
            "示例输入：「黄金 2000 美元/盎司，汇率 7.2，换算成人民币每克多少？」\n"
            "示例输出：\n"
            "1 盎司 = 31.1035 克，所以每克美元价 = 2000 ÷ 31.1035 ≈ 64.25 美元/克；再乘汇率得 64.25 × 7.2 ≈ 462.6 元/克。即约 462 元/克。"
        )
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]
        if history:
            for msg in history:
                messages.append({
                    "role": msg.role,
                    "content": msg.content
                })
        messages.append({"role": "user", "content": current_query})
        return messages
    
    # ========== 便捷方法 ==========
    
    def intent_only(
        self,
        query: str,
        conversation_id: Optional[str] = None,
        history_turns: int = 5,
        current_date: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        仅执行意图判断（兼容旧 API）
        
        Returns:
            意图分析结果字典
        """
        # 加载历史
        history = self._load_conversation_history(conversation_id, history_turns)
        
        # 改写
        rewrite_result = self.query_rewriter.rewrite(
            current_input=query,
            history=history,
        )
        standalone_query = rewrite_result.standalone_query

        # 分类
        classification = self.intent_classifier.classify(
            standalone_query=standalone_query,
            current_date=current_date,
            history=history,
            original_query=query,
        )
        
        # 转换为旧格式（filter_category 主类；filter_categories 为 top3）
        return {
            "needs_search": classification.needs_search,
            "intent_type": self._map_intent_type(classification.intent_type),
            "category": classification.filter_category,
            "filter_category": classification.filter_category,
            "filter_categories": getattr(classification, "filter_categories", None) or [classification.filter_category],
            "core_claim": standalone_query[:50],
            "is_historical": classification.time_sensitivity == "historical",
            "time_window": "",
            "resolved_date": "",
            "scope": "both",
            "key_metrics": "",
            "standalone_query": standalone_query,
            "time_sensitivity": classification.time_sensitivity,
            "confidence": classification.confidence
        }
    
    def _map_intent_type(self, intent_type: str) -> str:
        """映射意图类型到旧格式"""
        mapping = {
            "news": "新闻",
            "realtime_quote": "实时行情",
            "knowledge": "常识问答",
            "chitchat": "no_search",
            "tool": "工具"
        }
        return mapping.get(intent_type, intent_type)


# 工厂函数
_pipeline_instance: Optional[Pipeline] = None


def get_pipeline() -> Pipeline:
    """获取 Pipeline 单例"""
    global _pipeline_instance
    if _pipeline_instance is None:
        _pipeline_instance = Pipeline()
    return _pipeline_instance
