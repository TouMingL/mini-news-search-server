# app/services/pipeline.py
"""
Pipeline 编排器
职责：串联预处理层、分类层、决策层、执行层、反馈层
实现完整的 RAG 流水线

直接生成时「是否带历史」：若判定为闲聊且低置信度，或当前句与上一轮相关度低于
CONTEXT_RELEVANCE_THRESHOLD，则不带历史，仅用当前句生成，避免延续上一话题。
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
    LatencyMetrics,
    TemporalContext,
    classification_from_route_output,
)
from app.services.query_rewriter import QueryRewriter, get_query_rewriter
from app.services.temporal_resolver import TemporalResolver
from app.services.time_intent_classifier import TimeIntentClassifier, get_time_intent_classifier
from app.services.route_llm import get_route_llm
from app.services.local_llm_service import get_local_llm_service
from app.services.router import Router, get_router
from app.services.session_state import SessionStateManager, get_session_state_manager
from app.services.pipeline_logger import PipelineLogger, get_pipeline_logger
from app.services.pipeline_tracer import PipelineTracer
from app.services.temporal_scope import compute_answer_scope_date, compute_answer_scope_mode
from app.services.answer_verifier import (
    AnswerVerifier,
    VerificationResult,
    get_replacement_message,
    NO_EVIDENCE_FOR_DATE_MESSAGE,
)
from app.services.vector_store import make_dedup_key
from concurrent.futures import ThreadPoolExecutor
from app.services.pipeline_modules import (
    build_follow_up_temporal_context,
    classify_follow_up_type,
    _filter_by_semantic_score,
    _filter_published_on_date,
    _format_scores_reply,
    _slim_scores_context,
    _get_last_turn_category,
    _get_last_turn_user_input_from_history,
    _get_retrieval_min_semantic_score,
    _get_retrieval_term_overlap_boost_weight,
    _inject_date_into_query_for_search,
    _read_nba_scores_for_query,
    _sanitize_event,
    _term_overlap_ratio,
)

# 直接生成时：闲聊且置信度低于此值视为与上下文无关，不带历史（与 session_state.CONFIDENCE_FLOOR 语义一致）
CHITCHAT_CONTEXT_IRRELEVANT_CONFIDENCE = 0.5

from dataclasses import dataclass, field as dc_field

@dataclass
class PreprocessResult:
    """run / run_stream 公共前置流程的全部中间产物。"""
    history: List[Any]
    current_date: str
    temporal_context: Any
    time_intent: Any
    state: Any
    route_llm_output: Any
    parse_result: Any
    last_turn_category: Optional[str]
    follow_up_type: Optional[str]
    answer_scope_date: Optional[str]
    standalone_query: str
    rewrite_reasoning: Optional[str]
    rewrite_skipped: bool
    route_decision: Any
    classification: Any

class Pipeline:
    """
    RAG Pipeline 编排器
    
    数据流：
    UserInput -> QueryRewriter -> RouteLLM -> Router -> Executor -> Logger -> Response
    """
    
    def __init__(
        self,
        query_rewriter: QueryRewriter = None,
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
            router: 路由决策器
            state_manager: 会话状态管理器
            pipeline_logger: 日志记录器
            vector_store: 向量存储（检索用）
            llm_service: LLM服务（生成用）
        """
        self._query_rewriter = query_rewriter
        self._router = router
        self._state_manager = state_manager
        self._pipeline_logger = pipeline_logger
        self._vector_store = vector_store
        self._llm_service = llm_service
        self._answer_verifier = None
    
    # ========== 延迟初始化属性 ==========
    
    @property
    def answer_verifier(self) -> AnswerVerifier:
        if self._answer_verifier is None:
            self._answer_verifier = AnswerVerifier(self.llm_service)
        return self._answer_verifier
    
    @property
    def query_rewriter(self) -> QueryRewriter:
        if self._query_rewriter is None:
            self._query_rewriter = get_query_rewriter()
        return self._query_rewriter

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
            # 优先用 event_time_timestamp，否则 published_time，都没有则 inf
            event_ts = item.get("event_time_timestamp")
            published_time = item.get("published_time")
            if event_ts is not None:
                try:
                    anchor_ts = anchor_naive.timestamp()
                    item["_days_diff"] = abs(anchor_ts - float(event_ts)) / 86400.0
                except Exception:
                    item["_days_diff"] = float("inf")
            elif published_time:
                try:
                    pub_dt = datetime.fromisoformat(published_time)
                    pub_naive = pub_dt.replace(tzinfo=None)
                    item["_days_diff"] = abs((anchor_naive - pub_naive).total_seconds()) / 86400.0
                except Exception:
                    item["_days_diff"] = float("inf")
            else:
                item["_days_diff"] = float("inf")

        # 语义排名（优先用带字面重叠加分的 _semantic_rank_score，无则用 score）
        def _semantic_key(i):
            item = results[i]
            return item.get("_semantic_rank_score", item.get("score", 0))
        semantic_order = sorted(
            range(len(results)), key=_semantic_key, reverse=True
        )
        rank_sem = {i: rank for rank, i in enumerate(semantic_order)}

        # 时间排名（分层：同日=0，1天内=1，更远=大值，避免线性差值让远距离仍参与）
        def _time_tier(i):
            d = results[i].get("_days_diff", float("inf"))
            if d == float("inf"):
                return float("inf")
            if d <= 0:
                return 0
            if d <= 1.0:
                return 1.0
            return 100.0 + d
        time_order = sorted(
            range(len(results)), key=_time_tier
        )
        rank_time = {i: rank for rank, i in enumerate(time_order)}

        # RRF 融合
        for idx, item in enumerate(results):
            sem_rrf = 1.0 / (k + rank_sem[idx])
            time_rrf = 1.0 / (k + rank_time[idx])
            item["original_score"] = item.get("score", 0)
            item.pop("_semantic_rank_score", None)
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

    def _apply_term_overlap_boost(
        self,
        results: List[Dict[str, Any]],
        query: str,
    ) -> None:
        """
        按「查询-文档字面重叠」对每条结果的排序分加分，不修改原始 score（保留给 original_score 做语义过滤）。
        结果中会写入 _semantic_rank_score = score + boost_weight * overlap_ratio，供 _apply_time_rerank 用于语义排名。
        重叠判断基于 query 与 title+content 的字符集，不依赖词表。
        """
        weight = _get_retrieval_term_overlap_boost_weight()
        if weight <= 0 or not query or not results:
            return
        for item in results:
            ratio = _term_overlap_ratio(
                query,
                item.get("title") or "",
                item.get("content") or "",
            )
            base = item.get("score", 0)
            item["_semantic_rank_score"] = base + weight * ratio

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
        time_filter_strategy: Optional[str] = None,
        filter_event_time_from: Optional[str] = None,
        filter_event_time_to: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        检索：优先在前三类别 filter_categories 中搜；无则退化为单类 + 软提权。
        当 time_filter_strategy == event_time_with_fallback 且提供 filter_event_time_* 时，双路检索后合并去重。
        """
        fallback_query = standalone_query.replace("最新", "").replace("动态", "").replace("情况", "").strip()
        use_top3       = bool(filter_categories)
        effective_k    = top_k if use_top3 else (top_k * 3 if filter_category else top_k) # 3倍候选
        if len(search_queries) > 1: # 多 query 时拉更多候选
            fetch_k = min(effective_k * 2, 15) # 2倍候选，最多15条
        else:
            fetch_k = effective_k

        do_dual_path = (
            time_filter_strategy == "event_time_with_fallback"
            and filter_event_time_from is not None
            and filter_event_time_to is not None
        )
        if do_dual_path:
            # 问某天发生的事件
            # 路 A：event_time 在范围内，且 publish_time ∈ [event_date, event_date+1天]（当天或次日报道）
            list_a = self.vector_store.search_with_expansion(
                queries                =search_queries,
                top_k                  =fetch_k,
                filter_source          =filter_source,
                filter_category        =None if use_top3 else filter_category,
                filter_categories      =filter_categories,
                filter_date_from       =filter_date_from,
                filter_date_to         =filter_date_to,
                filter_event_time_from =filter_event_time_from,
                filter_event_time_to   =filter_event_time_to,
                fallback_query         =fallback_query if fallback_query != standalone_query else None, # 无 event_time 时 fallback
            )
            # 路 B：publish_time 在范围内（无 event_time 过滤，命中「无 event_time」或次日报道）
            list_b = self.vector_store.search_with_expansion(
                queries                =search_queries,
                top_k                  =fetch_k,
                filter_source          =filter_source,
                filter_category        =None if use_top3 else filter_category,
                filter_categories      =filter_categories,
                filter_date_from       =filter_date_from,
                filter_date_to         =filter_date_to,
                filter_event_time_from =None,
                filter_event_time_to   =None,
                fallback_query         =fallback_query if fallback_query != standalone_query else None,
            )
            try:
                # 解析 event_time 转换为 timestamp
                from datetime import datetime as _dt
                dt_from       = _dt.strptime(filter_event_time_from, "%Y-%m-%d")
                dt_to         = _dt.strptime(filter_event_time_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
                event_from_ts = dt_from.timestamp()
                event_to_ts   = dt_to.timestamp()
            except Exception:
                event_from_ts = float("-inf")
                event_to_ts = float("inf")
            
            # 根据解析结果去除 相同时间点的搜索结果（也就是同一条新闻）
            merged_by_key: Dict[str, Dict[str, Any]] = {}
            for item in list_a:
                merged_by_key[make_dedup_key(item)] = item
            for item in list_b:
                key = make_dedup_key(item)
                if key in merged_by_key:
                    continue
                et = item.get("event_time_timestamp")
                if et is not None and event_from_ts <= float(et) <= event_to_ts:
                    continue  # 被路 A 覆盖，去重
                merged_by_key[key] = item
            results = sorted(merged_by_key.values(), key=lambda x: x.get("score", 0), reverse=True)
        else:
            # 问某天的报道
            results = self.vector_store.search_with_expansion(
                queries                =search_queries,
                top_k                  =fetch_k,
                filter_source          =filter_source,
                filter_category        =None if use_top3 else filter_category,
                filter_categories      =filter_categories,
                filter_date_from       =filter_date_from,
                filter_date_to         =filter_date_to,
                filter_event_time_from =filter_event_time_from,
                filter_event_time_to   =filter_event_time_to,
                fallback_query         =fallback_query if fallback_query != standalone_query else None,
            )

        # 只搜特定类别时，为了避免数据源的分类错误，其他相关类别的新闻也返回（加权）
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
        合并原话与改写变体结果，避免原话被改写曲解，导致搜索结果偏移。
        将检索结果列表按 RRF (Reciprocal Rank Fusion) 融合。
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
        filter_source:          Optional[str],
        filter_category:        Optional[str],
        filter_categories:      Optional[List[str]] = None,
        filter_date_from:       Optional[str] = None,
        filter_date_to:         Optional[str] = None,
        time_filter_strategy:   Optional[str] = None,
        filter_event_time_from: Optional[str] = None,
        filter_event_time_to:   Optional[str] = None,
        original_query:         Optional[str] = None,
        reference_date:         Optional[str] = None,
        current_date:           Optional[str] = None,
    ) -> tuple:
        """
        拆解用户意图，生成子查询，每个子查询独立检索。
        判断是否需要拆分查询，每个子查询独立检索。
        单一意图且 original_query != standalone_query 时做双轨检索（原始 query 一轨 + 改写变体一轨）并 RRF 融合。

        Returns:
            (search_results, all_search_queries, covered_sub_queries, missed_sub_queries)
        """
        sub_queries = self.llm_service.decompose_query(standalone_query)

        if len(sub_queries) <= 1:
            # ---- 单一意图：改写变体检索 ----
            # 检索用查询将相对时间替换为具体日期，便于与新闻标题/正文匹配
            search_standalone_query = _inject_date_into_query_for_search(standalone_query, reference_date)
            search_queries = self.llm_service.expand_queries_for_search(
                search_standalone_query,
                num_variants=3,
                reference_date=reference_date,
                current_date=current_date,
            )
            logger.info(f"检索查询(扩展): {search_queries}")
            results_rewritten = self._search_hybrid(
                search_queries=search_queries,
                standalone_query=search_standalone_query,
                top_k=top_k,
                filter_source=filter_source,
                filter_category=filter_category,
                filter_categories=filter_categories,
                filter_date_from=filter_date_from,
                filter_date_to=filter_date_to,
                time_filter_strategy=time_filter_strategy,
                filter_event_time_from=filter_event_time_from,
                filter_event_time_to=filter_event_time_to,
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
                        filter_event_time_from=filter_event_time_from,
                        filter_event_time_to=filter_event_time_to,
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

        # Step 1: 生成各子查询的检索变体（子查询中的相对时间替换为具体日期）
        sub_variants: Dict[str, List[str]] = {}
        sub_q_to_search_query: Dict[str, str] = {}
        all_search_queries: List[str] = []
        for sub_q in sub_queries:
            search_sub_q = _inject_date_into_query_for_search(sub_q, reference_date)
            sub_q_to_search_query[sub_q] = search_sub_q
            variants = self.llm_service.expand_queries_for_search(
                search_sub_q,
                num_variants=2,
                reference_date=reference_date,
                current_date=current_date,
            )
            sub_variants[sub_q] = variants
            all_search_queries.extend(variants)
            logger.info(f"  子查询 '{sub_q}' 检索变体: {variants}")

        # Step 2: 并发执行各子查询的向量检索（使用日期注入后的 query 做 term overlap）
        sub_query_results: Dict[str, List[Dict]] = {}
        with ThreadPoolExecutor(max_workers=min(len(sub_queries), 4)) as executor:
            futures = {}
            for sub_q, variants in sub_variants.items():
                search_standalone = sub_q_to_search_query.get(sub_q, sub_q)
                futures[sub_q] = executor.submit(
                    self._search_hybrid,
                    search_queries=variants,
                    standalone_query=search_standalone,
                    top_k=top_k,
                    filter_source=filter_source,
                    filter_category=filter_category,
                    filter_categories=filter_categories,
                    filter_date_from=filter_date_from,
                    filter_date_to=filter_date_to,
                    time_filter_strategy=time_filter_strategy,
                    filter_event_time_from=filter_event_time_from,
                    filter_event_time_to=filter_event_time_to,
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

    # ========== 公共前置流程 ==========

    def _preprocess(
        self,
        input_data: PipelineInput,
        tracer: PipelineTracer,
        latency: LatencyMetrics,
    ) -> PreprocessResult:
        """
        run / run_stream 共享的前置流程：加载历史 -> 时间解析 -> RouteLLM -> 追问继承 -> 改写 -> 路由决策。
        所有 tracer.record_* 和 latency 计时在此完成，调用方只需拿到 PreprocessResult 进入执行层。
        """
        # 1.1 获取对话历史
        history = self._load_conversation_history(
            conversation_id=input_data.conversation_id,
            max_turns=input_data.history_turns
        )
        tracer.record_input(
            raw_query=input_data.query,
            conversation_id=input_data.conversation_id,
            history=history,
        )

        # 1.2 时间解析：将相对时间（昨天/前天/今天）解析为绝对日期（query_time）
        current_date     = input_data.current_date or datetime.now().strftime("%Y-%m-%d")
        ref_dt           = datetime.strptime(current_date, "%Y-%m-%d") if current_date else datetime.now()
        temporal_context = TemporalResolver.resolve(input_data.query, reference_time=ref_dt)
        if temporal_context.resolved and temporal_context.reference_date:
            kwargs = {**temporal_context.model_dump(), "query_time": temporal_context.reference_date, "time_source": "query"}
            temporal_context = TemporalContext(**kwargs)
        tracer.record_temporal(temporal_context)

        # 1.3 时间意图：若解析出日期则区分「昨日报道」vs「昨日发生的事」
        time_intent = None
        if temporal_context.resolved:
            classifier = get_time_intent_classifier()
            time_intent = classifier.classify(
                input_data.query,
                reference_date=temporal_context.reference_date,
                current_date=current_date,
            )
            tracer.record_time_intent(time_intent)

        # 2. 路由小 LLM：仅用户句 + 上轮类别，不输入 agent 回复
        state = self.state_manager.get_state(
            input_data.conversation_id or "anonymous"
        )
        last_turn_category = _get_last_turn_category(history)
        t0 = time.time()
        route_llm = get_route_llm()
        route_llm_output = route_llm.invoke(
            input_data.query,
            last_turn_category,
        )
        latency.classify_ms = (time.time() - t0) * 1000
        _parse_result = route_llm.last_parse_result
        tracer.record_route_llm(
            user_utterance=input_data.query,
            last_filter_category=last_turn_category,
            result_dict=route_llm_output.model_dump(),
            elapsed_ms=latency.classify_ms,
            parse_result_dict=_parse_result.model_dump() if _parse_result else None,
        )

        # Parser 时间实体回流：当 TemporalResolver 失败但 Parser LLM 提取到 time 实体时，
        # 用 time 实体值重新 resolve，弥补 regex 对嵌入式日期的盲区。
        if not temporal_context.resolved and _parse_result:
            time_entities = [e for e in _parse_result.entities if e.type == "time"]
            if time_entities:
                re_resolved = TemporalResolver.resolve(time_entities[0].value, reference_time=ref_dt)
                if re_resolved.resolved:
                    kwargs = {**re_resolved.model_dump(), "query_time": re_resolved.reference_date, "time_source": "query"}
                    temporal_context = TemporalContext(**kwargs)
                    logger.info("Parser 时间实体回流成功: '%s' -> reference_date=%s", time_entities[0].value, temporal_context.reference_date)
                    tracer.record_temporal(temporal_context)

        # 追问类型识别 + 时间继承（优先 RouteLLM.follow_up_time_type，否则规则 fallback）
        follow_up_type = None
        if last_turn_category:
            if temporal_context.resolved:
                follow_up_type = "time_switch"
            elif getattr(route_llm_output, "follow_up_time_type", None) is not None:
                follow_up_type = route_llm_output.follow_up_time_type
            else:
                follow_up_type = classify_follow_up_type(
                    input_data.query, temporal_context.resolved, last_turn_category
                )
        ctx_time_source = None
        ctx_temporal_context = None
        if follow_up_type == "time_switch":
            tracer.record_context_temporal(None, temporal_context, last_turn_category, follow_up_type)
        elif follow_up_type in ("event_continue", "object_switch") and history:
            result = build_follow_up_temporal_context(history, current_date)
            if result is not None:
                ctx_time_source, ctx_temporal_context = result
                temporal_context = ctx_temporal_context
                logger.info("追问场景使用历史推断 reference_date: {} (来源: {})", temporal_context.reference_date, ctx_time_source)
            tracer.record_context_temporal(ctx_time_source, ctx_temporal_context, last_turn_category, follow_up_type)

        answer_scope_date = compute_answer_scope_date(temporal_context, follow_up_type)

        # 3. 查询改写
        t0 = time.time()
        last_turn_user_input = _get_last_turn_user_input_from_history(history)
        rewrite_result = self.query_rewriter.rewrite(
            current_input=input_data.query,
            history=history,
            category_hint=route_llm_output.filter_category,
            last_standalone_query=last_turn_user_input,
            follow_up_type=follow_up_type,
        )
        latency.rewrite_ms = (time.time() - t0) * 1000
        standalone_query = rewrite_result.standalone_query
        rewrite_reasoning = rewrite_result.reasoning
        rewrite_skipped = (standalone_query.strip() == input_data.query.strip())
        rewrite_prompt = ""
        if not rewrite_skipped and history:
            history_text = self.query_rewriter._format_history(history)
            category_constraint = ""
            if route_llm_output.filter_category:
                category_constraint = (
                    f"当前用户问题已被判定属于「{route_llm_output.filter_category}」领域，"
                    "改写时请勿偏离该领域，仅做指代消解与信息补全。\n\n"
                )
            last_turn_line = self.query_rewriter._format_last_turn_user_input(
                last_turn_user_input, follow_up_type
            )
            if last_turn_line:
                last_turn_line = last_turn_line + "\n"
            rewrite_prompt = self.query_rewriter.REWRITE_PROMPT.format(
                history=history_text,
                last_turn_user_input_line=last_turn_line,
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

        # 4. 路由决策
        t0 = time.time()
        route_decision = self.router.decide(
            route_llm_output=route_llm_output,
            state=state,
            standalone_query=standalone_query,
            temporal_context=temporal_context,
            time_intent=time_intent,
            effective_last_category=last_turn_category,
        )
        latency.route_ms = (time.time() - t0) * 1000

        _player_filter = []
        if _parse_result:
            _player_filter = [e.value for e in _parse_result.entities if e.type == "player"]
            _is_player_stats = _parse_result.intent == "player_stats"
        else:
            _is_player_stats = False
        _detail_kw = (
            "细节", "详细", "具体", "更多", "补充", "数据", "统计",
            "展开", "细说", "多说", "多讲", "深入",
            "再细", "再详细", "再具体", "详细点", "具体点", "补充说明",
        )
        _detail_follow_up = (
            _is_player_stats
            or (
                getattr(route_llm_output, "follow_up_time_type", None) == "event_continue"
                and any(k in (input_data.query or "") for k in _detail_kw)
            )
        )
        if route_decision.search_params is None:
            route_decision.search_params = {}
        route_decision.search_params["detail_follow_up"] = _detail_follow_up
        route_decision.search_params["player_filter"] = _player_filter if _player_filter else None
        route_decision.search_params["follow_up_time_type"] = getattr(route_llm_output, "follow_up_time_type", None)
        route_decision.search_params["answer_scope_date"] = answer_scope_date
        route_decision.search_params["scores_intent"] = _parse_result.intent if _parse_result else None
        route_decision.search_params["queried_teams"] = (
            [e.value for e in _parse_result.entities if e.type == "team"]
            if _parse_result else None
        )
        tracer.record_route(
            action=route_decision.action,
            reason=route_decision.reason,
            elapsed_ms=latency.route_ms,
            search_params=route_decision.search_params,
        )

        classification = classification_from_route_output(route_llm_output)

        return PreprocessResult(
            history=history,
            current_date=current_date,
            temporal_context=temporal_context,
            time_intent=time_intent,
            state=state,
            route_llm_output=route_llm_output,
            parse_result=_parse_result,
            last_turn_category=last_turn_category,
            follow_up_type=follow_up_type,
            answer_scope_date=answer_scope_date,
            standalone_query=standalone_query,
            rewrite_reasoning=rewrite_reasoning,
            rewrite_skipped=rewrite_skipped,
            route_decision=route_decision,
            classification=classification,
        )

    # ========== 主方法 ==========
    
    def run(self, input_data: PipelineInput) -> PipelineOutput:
        """
        同步执行完整的 Pipeline 流程（预处理 -> 执行 -> 状态更新 -> 日志）。
        """
        start_time = time.time()
        request_id = self.pipeline_logger.create_request_id()
        tracer     = PipelineTracer(request_id)
        latency    = LatencyMetrics()
        
        try:
            ctx = self._preprocess(input_data, tracer, latency)

            # 5. 执行层
            t0 = time.time()
            answer, sources, retrieval_count, evidence_ok, verification_result = self._execute(
                route_decision=ctx.route_decision,
                standalone_query=ctx.standalone_query,
                original_query=input_data.query,
                input_data=input_data,
                history=ctx.history,
                tracer=tracer,
                rewrite_reasoning=ctx.rewrite_reasoning,
                classification=ctx.classification,
                temporal_context=ctx.temporal_context,
            )
            latency.retrieve_ms = (time.time() - t0) * 1000 if ctx.route_decision.action == "search_then_generate" else 0
            latency.generate_ms = (time.time() - t0) * 1000 - latency.retrieve_ms
            
            # 6. 更新会话状态
            self.state_manager.update_state(
                conversation_id=input_data.conversation_id or "anonymous",
                classification=ctx.classification,
                route_action=ctx.route_decision.action
            )
            
            # 7. 记录日志
            latency.total_ms = (time.time() - start_time) * 1000
            self.pipeline_logger.log(
                request_id=request_id,
                conversation_id=input_data.conversation_id,
                raw_input=input_data.query,
                standalone_query=ctx.standalone_query,
                classification=ctx.classification,
                route_decision=ctx.route_decision,
                retrieval_count=retrieval_count,
                final_response=answer,
                latency=latency
            )
            
            tracer.record_glm_output(
                answer=answer,
                verified=verification_result.passed if verification_result else True,
                failure_reason=verification_result.failure_reason if verification_result else None,
                evidence_ok=evidence_ok,
                verification_result=verification_result,
            )
            tracer.flush(total_ms=latency.total_ms)
            
            return PipelineOutput(
                answer=answer,
                sources=sources,
                classification=ctx.classification,
                route_decision=ctx.route_decision,
                standalone_query=ctx.standalone_query,
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
                    need_retrieval=True,
                    need_scores=False,
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
        start_time         = time.time()
        request_id         = self.pipeline_logger.create_request_id()
        tracer             = PipelineTracer(request_id)
        latency            = LatencyMetrics()
        accumulated_answer = ""
        ctx                = None
        retrieval_count    = 0
        final_sources      = []
        try:
            ctx = self._preprocess(input_data, tracer, latency)

            # 执行层（搜索 + 生成 / 直接生成）
            t0 = time.time()
            raw_stream_answer = ""
            stream_verification_result = None
            for event in self._execute_stream(
                route_decision=ctx.route_decision,
                standalone_query=ctx.standalone_query,
                original_query=input_data.query,
                input_data=input_data,
                history=ctx.history,
                tracer=tracer,
                rewrite_reasoning=ctx.rewrite_reasoning,
                classification=ctx.classification,
                temporal_context=ctx.temporal_context,
            ):
                if "choices" in event and event["choices"]:
                    delta = event["choices"][0].get("delta") or {}
                    content = delta.get("content")
                    if isinstance(content, str):
                        accumulated_answer += content
                        raw_stream_answer += content
                if "replace" in event:
                    accumulated_answer = event["replace"]
                    stream_verification_result = event.get("verification_result")
                if event.get("done"):
                    final_sources = event.get("sources") or []
                    retrieval_count = len(final_sources)
                yield {k: v for k, v in event.items() if k != "verification_result"}
            if accumulated_answer:
                logger.info("agent 回复:\n{}", accumulated_answer)
            latency.retrieve_ms = (time.time() - t0) * 1000 if ctx.route_decision.action == "search_then_generate" else 0
            latency.generate_ms = (time.time() - t0) * 1000 - (latency.retrieve_ms if ctx.route_decision.action == "search_then_generate" else 0)
            latency.total_ms = (time.time() - start_time) * 1000
            self.state_manager.update_state(
                conversation_id=input_data.conversation_id or "anonymous",
                classification=ctx.classification,
                route_action=ctx.route_decision.action
            )
            self.pipeline_logger.log(
                request_id=request_id,
                conversation_id=input_data.conversation_id,
                raw_input=input_data.query,
                standalone_query=ctx.standalone_query,
                classification=ctx.classification,
                route_decision=ctx.route_decision,
                retrieval_count=retrieval_count,
                final_response=accumulated_answer,
                latency=latency
            )
            evidence_ok_stream = (
                True if stream_verification_result is not None
                else (False if accumulated_answer == NO_EVIDENCE_FOR_DATE_MESSAGE else None)
            )
            tracer.record_glm_output(
                answer=accumulated_answer,
                raw_stream=raw_stream_answer,
                verified=stream_verification_result.passed if stream_verification_result else True,
                failure_reason=stream_verification_result.failure_reason if stream_verification_result else None,
                evidence_ok=evidence_ok_stream,
                verification_result=stream_verification_result,
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
                standalone_query=(ctx.standalone_query if ctx else input_data.query) or "",
                classification=(ctx.classification if ctx else None) or ClassificationResult(
                    needs_search=True, need_retrieval=True, need_scores=False,
                    intent_type="news", filter_category="general",
                    filter_categories=["general"], time_sensitivity="none", confidence=0.0
                ),
                route_decision=(ctx.route_decision if ctx else None) or RouteDecision(action="fallback", reason=str(e)),
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

    def _get_context_relevance_threshold(self) -> float:
        """读取「当前句与上下文相关度」阈值，低于此值则直接生成时不带历史。"""
        try:
            from flask import current_app
            return float(current_app.config.get("CONTEXT_RELEVANCE_THRESHOLD", 0.45))
        except Exception:
            return 0.45

    def _compute_context_relevance(self, query: str, history: List[HistoryMessage]) -> float:
        """
        当前 query 与上一轮对话的语义相关度（0~1）。
        用于判断是否「与上下文无关」：低于阈值则仅用新消息回复。
        """
        if not history or len(history) < 2:
            return 1.0
        last_turn = history[-2:]
        ctx_parts = [m.content.strip() for m in last_turn if getattr(m, "content", None)]
        if not ctx_parts:
            return 1.0
        ctx_text = " ".join(ctx_parts)
        if not query.strip() or not ctx_text:
            return 1.0
        try:
            emb_q = self.vector_store.embedding_service.encode_query(query.strip(), normalize_embeddings=True)
            emb_ctx = self.vector_store.embedding_service.encode_query(ctx_text, normalize_embeddings=True)
            if len(emb_q) != len(emb_ctx):
                return 0.0
            sim = sum(a * b for a, b in zip(emb_q, emb_ctx))
            return max(0.0, min(1.0, sim))
        except Exception as e:
            logger.warning(f"相关度计算失败，视为无关: {e}")
            return 0.0

    def _resolve_effective_history_for_direct(
        self,
        classification: Optional[ClassificationResult],
        query: str,
        history: Optional[List[HistoryMessage]],
    ) -> List[HistoryMessage]:
        """
        直接生成时决定是否带历史：与上下文无关则返回空列表，仅用新消息回复。
        第一层：chitchat 且低置信度 -> 不带历史；
        第二层：当前 query 与上一轮语义相关度 < 阈值 -> 不带历史。
        """
        history = history or []
        if len(history) < 2:
            return history
        if classification:
            if classification.intent_type == "chitchat" and classification.confidence < CHITCHAT_CONTEXT_IRRELEVANT_CONFIDENCE:
                logger.info("直接生成不带历史: 闲聊且低置信度")
                return []
        threshold = self._get_context_relevance_threshold()
        relevance = self._compute_context_relevance(query, history)
        if relevance < threshold:
            logger.info(f"直接生成不带历史: 相关度={relevance:.4f} < {threshold}")
            return []
        return history

    def _execute(
        self,
        route_decision:     RouteDecision,
        standalone_query:   str,
        original_query:     str,
        input_data:         PipelineInput,
        history:            Optional[List[HistoryMessage]] = None,
        tracer:             Optional[PipelineTracer] = None,
        rewrite_reasoning:  Optional[str] = None,
        classification:     Optional[ClassificationResult] = None,
        temporal_context:   Optional[TemporalContext] = None,
    ) -> tuple[str, List[Dict[str, Any]], int, Optional[bool], Optional[VerificationResult]]:
        """
        执行路由决策

        Returns:
            (answer, sources, retrieval_count, evidence_ok, verification_result)
            evidence_ok 仅在有 reference_date 的检索生成路径下表示「时间证据」是否存在；
            verification_result 为 AnswerVerifier 结果，未做校验时为 None。
        """
        if route_decision.action == "search_then_generate":
            return self._execute_search_then_generate(
                route_decision=route_decision,
                standalone_query=standalone_query,
                original_query=original_query,
                input_data=input_data,
                tracer=tracer,
                rewrite_reasoning=rewrite_reasoning,
                classification=classification,
                temporal_context=temporal_context,
            )

        elif route_decision.action == "generate_direct":
            effective_history = self._resolve_effective_history_for_direct(
                classification=classification,
                query=original_query or standalone_query,
                history=history,
            )
            return self._execute_generate_direct(
                original_query=original_query,
                history=effective_history,
                tracer=tracer,
                route_decision=route_decision,
            ) + (None, None)
        elif route_decision.action in ("tool_quote", "tool_weather"):
            # 工具未接入：明确拒绝，不做幻觉生成
            tool_name   = route_decision.action.replace("tool_", "")
            refusal     = f"抱歉，{tool_name}工具暂未接入，无法为你获取实时数据。"
            logger.warning(f"工具调用未实现，明确拒绝: {route_decision.action}")
            return refusal, [], 0, None, None

        elif route_decision.action == "tool_scores":
            # 赛况数据引擎：追问场景用推断的 reference_date（比赛日）；细节追问时强制输出节次+球员
            ref_date    = temporal_context.reference_date if temporal_context else None
            cur_date    = ref_date or input_data.current_date or datetime.now().strftime("%Y-%m-%d")
            score_query = original_query or standalone_query or ""
            _intent     = (route_decision.search_params or {}).get("scores_intent")
            _qt         = (route_decision.search_params or {}).get("queried_teams")
            data, was_filtered = _read_nba_scores_for_query(cur_date, score_query, intent=_intent)
            want_detail = (route_decision.search_params or {}).get("detail_follow_up", False)
            _pf         = (route_decision.search_params or {}).get("player_filter")
            # 按查询意图裁剪数据，再交 LLM 生成精准答案
            ctx      = _slim_scores_context(data, score_query, intent=_intent,
                                            was_filtered=was_filtered, want_detail=want_detail,
                                            player_filter=_pf, queried_teams=_qt)
            messages = self._build_scores_answer_messages(score_query, ctx)
            if tracer:
                tracer.record_glm_prompt(
                    system_prompt=messages[0]["content"],
                    user_prompt=messages[1]["content"],
                )
            answer = self.llm_service.chat(messages, max_tokens=600)
            if tracer:
                tracer.record_glm_output(answer, verified=True)
            return answer, [], 0, None, None

        # Fallback：未知路由动作，按直接生成处理
        effective_history = self._resolve_effective_history_for_direct(
                classification=classification,
                query=original_query or standalone_query,
                history=history,
            )
        return self._execute_generate_direct(
                original_query=original_query,
                history=effective_history,
                tracer=tracer,
                route_decision=route_decision,
            ) + (None, None)

    def _execute_stream(
        self,
        route_decision:     RouteDecision,
        standalone_query:   str,
        original_query:     str,
        input_data:         PipelineInput,
        history:            Optional[List[HistoryMessage]] = None,
        tracer:             Optional[PipelineTracer] = None,
        rewrite_reasoning:  Optional[str] = None,
        classification:     Optional[ClassificationResult] = None,
        temporal_context:   Optional[TemporalContext] = None,
    ) -> Iterator[Dict[str, Any]]:
        """执行层流式版本，yield SSE 事件 dict（content / replace / done）。"""
        if route_decision.action == "search_then_generate":
            search_params           = route_decision.search_params or {}
            filter_category         = input_data.filter_category or search_params.get("filter_category")
            filter_categories       = search_params.get("filter_categories")
            filter_source           = input_data.filter_source or search_params.get("filter_source")
            filter_date_from        = input_data.filter_date_from or search_params.get("filter_date_from")
            filter_date_to          = input_data.filter_date_to or search_params.get("filter_date_to")
            time_filter_strategy    = search_params.get("time_filter_strategy")
            filter_event_time_from  = search_params.get("filter_event_time_from")
            filter_event_time_to    = search_params.get("filter_event_time_to")
            ref_date                = temporal_context.reference_date if temporal_context else None
            answer_scope_date       = search_params.get("answer_scope_date")
            cur_date                = input_data.current_date or datetime.now().strftime("%Y-%m-%d")
            # 查询分解 + 并发检索（多意图查询每个子查询拥有独立 top_k 配额）；category 为 top3 时在前三类别中搜
            search_results, search_queries, covered, missed, asymmetry_note = self._search_decomposed(
                standalone_query=standalone_query,
                top_k=input_data.top_k,
                filter_source=filter_source,
                filter_category=filter_category,
                filter_categories=filter_categories,
                filter_date_from=filter_date_from,
                filter_date_to=filter_date_to,
                time_filter_strategy=time_filter_strategy,
                filter_event_time_from=filter_event_time_from,
                filter_event_time_to=filter_event_time_to,
                original_query=original_query,
                reference_date=ref_date,
                current_date=cur_date,
            )
            coverage_note = self._build_coverage_note(covered, missed)
            if asymmetry_note:
                coverage_note = (coverage_note + "\n" + asymmetry_note) if coverage_note else asymmetry_note
            if coverage_note:
                logger.info(f"检索覆盖情况:\n{coverage_note}")
            # 字面重叠加分（使用日期注入后的查询以匹配新闻正文）
            search_standalone_for_boost = _inject_date_into_query_for_search(standalone_query, ref_date)
            self._apply_term_overlap_boost(search_results, search_standalone_for_boost)
            # RRF 时间重排
            anchor_date, time_alpha = self._resolve_time_rerank_params(search_params, input_data.current_date)
            search_results          = self._apply_time_rerank(search_results, anchor_date, time_alpha)
            pre_sem_results         = list(search_results)
            min_sem                 = _get_retrieval_min_semantic_score()
            search_results          = _filter_by_semantic_score(search_results, min_sem)
            if min_sem > 0 and search_results:
                logger.info(f"语义分过滤: 阈值={min_sem}, 保留 {len(search_results)} 条 (过滤前 {len(pre_sem_results)} 条)")
            logger.info(
                f"RRF 时间重排: anchor={anchor_date.strftime('%Y-%m-%d')}, "
                f"time_alpha={time_alpha}"
            )
            logger.info(f"RAG 搜索结果(流式): 共 {len(search_results)} 条")
            for i, item in enumerate(search_results, 1):
                orig  = item.get('original_score')
                tw    = item.get('time_weight')
                extra = f" (sem={orig:.4f}, tw={tw:.4f})" if orig is not None else ""
                logger.info(
                    f"  [{i}] score={item.get('score', 0):.6f}{extra} | {item.get('published_time', '')} | {item.get('source', '')} | {item.get('title', '')[:60]}"
                )
            # 编排层：need_retrieval 且 need_scores 时，将赛况数据引擎结果并入 context 供生成与事实核查
            if classification and getattr(classification, "need_retrieval", False) and getattr(classification, "need_scores", False):
                try:
                    cur_date           = ref_date or input_data.current_date or datetime.now().strftime("%Y-%m-%d")
                    score_query        = original_query or standalone_query or ""
                    _intent            = (route_decision.search_params or {}).get("scores_intent")
                    _qt                = (route_decision.search_params or {}).get("queried_teams")
                    data, was_filtered = _read_nba_scores_for_query(cur_date, score_query, intent=_intent)
                    want_detail        = search_params.get("detail_follow_up", False)
                    _pf                = search_params.get("player_filter")
                    scores_text        = _format_scores_reply(data, include_detail=was_filtered or want_detail, player_detail=want_detail, player_filter=_pf, queried_teams=_qt)
                    search_results.append({
                        "title":  "NBA比分",
                        "source": "赛况数据引擎",
                        "content": scores_text,
                        "published_time": "",
                        "link": "",
                        "category": "sports",
                    })
                    logger.info("体育检索附带赛况数据引擎数据，已加入 context 供生成与事实核查")
                except Exception as e:
                    logger.warning("赛况数据引擎读取失败，跳过注入: %s", e)
            # --- 目标日 if-else 分支：事件结果==0 → 从过滤前恢复目标日报道 → 重新判定 mode ---
            answer_scope_mode = compute_answer_scope_mode(
                context=search_results,
                answer_scope_date=answer_scope_date,
                current_date=cur_date,
            )
            if answer_scope_date and answer_scope_mode != "report_day_ok" and not search_results:
                fallback = _filter_published_on_date(pre_sem_results, answer_scope_date)
                if fallback:
                    search_results = fallback
                    answer_scope_mode = compute_answer_scope_mode(
                        context=search_results,
                        answer_scope_date=answer_scope_date,
                        current_date=cur_date,
                    )
                    logger.info(
                        "目标日降级: 语义过滤后 0 条, 从过滤前 {} 条中恢复 {} 条目标日报道 -> {}",
                        len(pre_sem_results), len(fallback), answer_scope_mode,
                    )
            logger.info("answer_scope_mode={}", answer_scope_mode)
            # -- trace: 搜索结果（含完整正文）--
            if tracer:
                tracer.record_search(
                    search_queries=search_queries,
                    results=search_results,
                    anchor_date=anchor_date,
                    time_alpha=time_alpha,
                    retrieval_mode=getattr(self.vector_store, "_last_retrieval_mode", None),
                    filter_date_from=filter_date_from,
                    filter_date_to=filter_date_to,
                    reference_date=ref_date or search_params.get("reference_datetime"),
                    answer_scope_mode=answer_scope_mode,
                )
            if not search_results:
                for event in self.llm_service.generate_no_result_reply_stream(
                    query=standalone_query,
                    reference_date=answer_scope_date,
                    current_date=cur_date,
                ):
                    if "choices" in event and event.get("choices"):
                        yield _sanitize_event(event)
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
                    detail_follow_up=search_params.get("detail_follow_up", False),
                    reference_date=answer_scope_date,
                    current_date=cur_date,
                    answer_scope_mode=answer_scope_mode,
                )
                _full_user = f"{_user_text}\n\nRead again:\n{_user_text}"
                tracer.record_glm_prompt(
                    system_prompt=_sys_prompt,
                    user_prompt=_full_user,
                )
            deep_think = getattr(input_data, 'deep_think', False)
            stream_accumulated = []
            for event in self.llm_service.generate_answer_stream(
                query=standalone_query,
                context=search_results,
                deep_think=deep_think,
                coverage_note=coverage_note,
                original_query=original_query,
                rewrite_reasoning=rewrite_reasoning,
                reference_date=answer_scope_date,
                current_date=cur_date,
                detail_follow_up=search_params.get("detail_follow_up", False),
                answer_scope_mode=answer_scope_mode,
            ):
                if "replace" in event:
                    yield _sanitize_event(event)
                    yield _sanitize_event({"sources": sources, "done": True})
                    return
                if "choices" in event and event.get("choices"):
                    delta = event["choices"][0].get("delta") or {}
                    content = delta.get("content")
                    if isinstance(content, str):
                        stream_accumulated.append(content)
                yield _sanitize_event(event)
            full_answer = "".join(stream_accumulated)
            result = self.answer_verifier.verify(
                query=standalone_query,
                answer=full_answer,
                context=search_results,
                reference_date=answer_scope_date,
                current_date=cur_date,
                answer_scope_mode=answer_scope_mode,
            )
            if not result.passed:
                yield _sanitize_event({"replace": get_replacement_message(result.failure_reason), "verification_result": result})
            else:
                final = self.llm_service.post_process_answer(full_answer, search_results)
                yield _sanitize_event({"replace": final, "verification_result": result})
            yield _sanitize_event({"sources": sources, "done": True})
        elif route_decision.action in ("tool_quote", "tool_weather"):
            # 工具未接入：明确拒绝，不做幻觉生成
            tool_name = route_decision.action.replace("tool_", "")
            refusal   = f"抱歉，{tool_name}工具暂未接入，无法为你获取实时数据。"
            logger.warning(f"工具调用未实现，明确拒绝: {route_decision.action}")
            yield _sanitize_event({"choices": [{"delta": {"content": refusal}}]})
            yield _sanitize_event({"sources": [], "done": True})
        elif route_decision.action == "tool_scores":
            ref_date           = temporal_context.reference_date if temporal_context else None
            cur_date           = ref_date or input_data.current_date or datetime.now().strftime("%Y-%m-%d")
            score_query        = original_query or standalone_query or ""
            _intent            = (route_decision.search_params or {}).get("scores_intent")
            _qt                = (route_decision.search_params or {}).get("queried_teams")
            data, was_filtered = _read_nba_scores_for_query(cur_date, score_query, intent=_intent)
            want_detail        = (route_decision.search_params or {}).get("detail_follow_up", False)
            _pf                = (route_decision.search_params or {}).get("player_filter")
            # 按查询意图裁剪数据，再流式交 LLM 生成精准答案
            ctx      = _slim_scores_context(data, score_query, intent=_intent,
                                            was_filtered=was_filtered, want_detail=want_detail,
                                            player_filter=_pf, queried_teams=_qt)
            messages = self._build_scores_answer_messages(score_query, ctx)
            if tracer:
                tracer.record_glm_prompt(
                    system_prompt=messages[0]["content"],
                    user_prompt=messages[1]["content"],
                )
            raw_chunks: List[str] = []
            for chunk in self.llm_service.chat_stream(messages, max_tokens=600):
                raw_chunks.append(chunk)
                yield _sanitize_event({"choices": [{"delta": {"content": chunk}}]})
            if tracer:
                tracer.record_glm_output("".join(raw_chunks), verified=True)
            yield _sanitize_event({"sources": [], "done": True})
        else:
            # generate_direct / unknown action -> 直接生成（带历史或仅新消息）
            if route_decision.reason and "查询无效" in route_decision.reason:
                yield _sanitize_event({"choices": [{"delta": {"content": "未理解您的问题，请换个说法试试。"}}]})
                yield _sanitize_event({"sources": [], "done": True})
                return
            effective_history = self._resolve_effective_history_for_direct(
                classification=classification,
                query=original_query or standalone_query,
                history=history,
            )
            deep_think = getattr(input_data, 'deep_think', False)
            messages = self._build_chat_messages(original_query, effective_history)
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
        route_decision:     RouteDecision,
        standalone_query:   str,
        original_query:     str,
        input_data:         PipelineInput,
        tracer:             Optional[PipelineTracer] = None,
        rewrite_reasoning:  Optional[str] = None,
        classification:     Optional[ClassificationResult] = None,
        temporal_context:   Optional[TemporalContext] = None,
    ) -> tuple[str, List[Dict[str, Any]], int, Optional[bool], Optional[VerificationResult]]:
        """执行检索后生成"""
        # 合并检索参数
        search_params = route_decision.search_params or {}
        
        # 优先使用输入参数中的过滤条件
        filter_category   = input_data.filter_category or search_params.get("filter_category")
        filter_categories = search_params.get("filter_categories")
        filter_source     = input_data.filter_source or search_params.get("filter_source")
        filter_date_from  = input_data.filter_date_from or search_params.get("filter_date_from")
        filter_date_to    = input_data.filter_date_to or search_params.get("filter_date_to")
        
        # 1. 查询分解 + 并发检索（category 为 top3 时在前三类别中搜）
        ref_date          = temporal_context.reference_date if temporal_context else None
        answer_scope_date = search_params.get("answer_scope_date")
        current_date      = input_data.current_date or datetime.now().strftime("%Y-%m-%d")
        search_results, search_queries, covered, missed, asymmetry_note = self._search_decomposed(
            standalone_query=standalone_query,
            top_k=input_data.top_k,
            filter_source=filter_source,
            filter_category=filter_category,
            filter_categories=filter_categories,
            filter_date_from=filter_date_from,
            filter_date_to=filter_date_to,
            time_filter_strategy=search_params.get("time_filter_strategy"),
            filter_event_time_from=search_params.get("filter_event_time_from"),
            filter_event_time_to=search_params.get("filter_event_time_to"),
            original_query=original_query,
            reference_date=ref_date,
            current_date=current_date,
        )
        coverage_note = self._build_coverage_note(covered, missed)
        if asymmetry_note:
            coverage_note = (coverage_note + "\n" + asymmetry_note) if coverage_note else asymmetry_note
        if coverage_note:
            logger.info(f"检索覆盖情况:\n{coverage_note}")
        # 字面重叠加分（用于排序，不改变 original_score 语义分）；使用日期注入后的查询以匹配新闻正文
        search_standalone_for_boost = _inject_date_into_query_for_search(standalone_query, ref_date)
        self._apply_term_overlap_boost(search_results, search_standalone_for_boost)
        # RRF 时间重排
        anchor_date, time_alpha = self._resolve_time_rerank_params(
            search_params, input_data.current_date
        )
        search_results = self._apply_time_rerank(
            search_results, anchor_date, time_alpha
        )
        pre_sem_results = list(search_results)
        min_sem = _get_retrieval_min_semantic_score()
        search_results = _filter_by_semantic_score(search_results, min_sem)
        if min_sem > 0 and search_results:
            logger.info(f"语义分过滤: 阈值={min_sem}, 保留 {len(search_results)} 条 (过滤前 {len(pre_sem_results)} 条)")
        logger.info(
            f"RRF 时间重排: anchor={anchor_date.strftime('%Y-%m-%d')}, "
            f"time_alpha={time_alpha}"
        )
        
        logger.info(f"RAG 搜索结果(同步): 共 {len(search_results)} 条")
        for i, item in enumerate(search_results, 1):
            orig = item.get('original_score')
            tw = item.get('time_weight')
            extra = f" (sem={orig:.4f}, tw={tw:.4f})" if orig is not None else ""
            logger.info(
                f"  [{i}] score={item.get('score', 0):.6f}{extra} | {item.get('published_time', '')} | {item.get('source', '')} | {item.get('title', '')[:60]}"
            )
        
        # 编排层：need_retrieval 且 need_scores 时，将赛况数据引擎结果并入 context 供生成与事实核查
        if classification and getattr(classification, "need_retrieval", False) and getattr(classification, "need_scores", False):
            try:
                cur_date           = ref_date or input_data.current_date or datetime.now().strftime("%Y-%m-%d")
                score_query        = original_query or standalone_query or ""
                _intent            = (route_decision.search_params or {}).get("scores_intent")
                _qt                = (route_decision.search_params or {}).get("queried_teams")
                data, was_filtered = _read_nba_scores_for_query(cur_date, score_query, intent=_intent)
                want_detail        = search_params.get("detail_follow_up", False)
                _pf                = search_params.get("player_filter")
                scores_text        = _format_scores_reply(data, include_detail=was_filtered or want_detail, player_detail=want_detail, player_filter=_pf, queried_teams=_qt)
                search_results.append({
                    "title":  "NBA比分",
                    "source": "赛况数据引擎",
                    "content": scores_text,
                    "published_time": "",
                    "link": "",
                    "category": "sports",
                })
                logger.info("体育检索附带赛况数据引擎数据，已加入 context 供生成与事实核查")
            except Exception as e:
                logger.warning("赛况数据引擎读取失败，跳过注入: %s", e)

        # --- 目标日 if-else 分支：事件结果==0 → 从过滤前恢复目标日报道 → 重新判定 mode ---
        answer_scope_mode = compute_answer_scope_mode(
            context=search_results,
            answer_scope_date=answer_scope_date,
            current_date=current_date,
        )
        if answer_scope_date and answer_scope_mode != "report_day_ok" and not search_results:
            fallback = _filter_published_on_date(pre_sem_results, answer_scope_date)
            if fallback:
                search_results = fallback
                answer_scope_mode = compute_answer_scope_mode(
                    context=search_results,
                    answer_scope_date=answer_scope_date,
                    current_date=current_date,
                )
                logger.info(
                    "目标日降级: 语义过滤后 0 条, 从过滤前 {} 条中恢复 {} 条目标日报道 -> {}",
                    len(pre_sem_results), len(fallback), answer_scope_mode,
                )
        logger.info("answer_scope_mode={}", answer_scope_mode)

        # -- trace: 搜索结果 --
        if tracer:
            tracer.record_search(
                search_queries=search_queries,
                results=search_results,
                anchor_date=anchor_date,
                time_alpha=time_alpha,
                retrieval_mode=getattr(self.vector_store, "_last_retrieval_mode", None),
                filter_date_from=filter_date_from,
                filter_date_to=filter_date_to,
                reference_date=ref_date or search_params.get("reference_datetime"),
                answer_scope_mode=answer_scope_mode,
            )
        
        if not search_results:
            reply = self.llm_service.generate_no_result_reply(
                query=standalone_query,
                reference_date=answer_scope_date,
                current_date=current_date,
            )
            return reply, [], 0

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
                detail_follow_up=search_params.get("detail_follow_up", False),
                reference_date=answer_scope_date,
                current_date=current_date,
                answer_scope_mode=answer_scope_mode,
            )
            _full_user = f"{_user_text}\n\nRead again:\n{_user_text}"
            tracer.record_glm_prompt(
                system_prompt=_sys_prompt,
                user_prompt=_full_user,
            )
        
        # 3. LLM 生成回答（仅生成，不在此处校验）
        answer = self.llm_service.generate_answer(
            query=standalone_query,
            context=search_results,
            coverage_note=coverage_note,
            original_query=original_query,
            rewrite_reasoning=rewrite_reasoning,
            reference_date=answer_scope_date,
            current_date=current_date,
            detail_follow_up=search_params.get("detail_follow_up", False),
            answer_scope_mode=answer_scope_mode,
        )

        evidence_ok = self.llm_service.has_evidence_for_date(search_results, answer_scope_date, current_date) if answer_scope_date else None

        if answer == NO_EVIDENCE_FOR_DATE_MESSAGE:
            sources = [
                {"title": item.get("title"), "source": item.get("source"), "category": item.get("category"),
                 "link": item.get("link"), "score": item.get("score"), "published_time": item.get("published_time")}
                for item in search_results
            ]
            return answer, sources, len(search_results), False, None

        result = self.answer_verifier.verify(
            query=standalone_query,
            answer=answer,
            context=search_results,
            reference_date=answer_scope_date,
            current_date=current_date,
            answer_scope_mode=answer_scope_mode,
        )
        if not result.passed:
            answer = get_replacement_message(result.failure_reason)
        else:
            answer = self.llm_service.post_process_answer(answer, search_results)
        
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
        
        return answer, sources, len(search_results), evidence_ok, result
    
    def _execute_generate_direct(
        self,
        original_query: str,
        history: Optional[List[HistoryMessage]] = None,
        tracer: Optional[PipelineTracer] = None,
        route_decision: Optional[RouteDecision] = None,
    ) -> tuple[str, List[Dict[str, Any]], int]:
        """执行直接生成（不检索），带对话历史以支持多轮。若路由原因为「查询无效」则直接返回澄清文案不调 LLM。"""
        if route_decision and "查询无效" in (route_decision.reason or ""):
            return "未理解您的问题，请换个说法试试。", [], 0
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
    def _build_scores_answer_messages(
        query: str,
        scores_context: str,
    ) -> List[Dict[str, str]]:
        """
        基于已裁剪的赛况数据上下文构建 LLM 消息，用于生成针对用户问题的精准回答。

        系统提示要求 LLM 仅凭提供的数据作答，直接回答问题，不展示冗余数据，不编造。
        """
        system = (
            "你叫菠萝包，是专业的 NBA 数据播报助手。\n"
            "规则（严格遵守）：\n"
            "1. 只基于下方「赛况数据」回答，严禁使用训练知识补充任何数据。\n"
            "2. 直接回答用户问题，不重复列出与问题无关的条目。\n"
            "3. 回答时把命中条目的关键数据一并带出（战绩、胜率、连胜/连败、得分等），"
            "让用户一句话获得完整信息，但不要罗列无关条目。\n"
            "4. 数据不足以回答时，如实说明「当前数据不足以回答该问题」。\n"
            "5. 用中文作答，一两句话说清楚。"
        )
        user = f"赛况数据：\n{scores_context}\n\n用户问题：{query}"
        return [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ]

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
            "请用中文简洁准确地回答。"
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


# 工厂函数
_pipeline_instance: Optional[Pipeline] = None


def get_pipeline() -> Pipeline:
    """获取 Pipeline 单例"""
    global _pipeline_instance
    if _pipeline_instance is None:
        _pipeline_instance = Pipeline()
    return _pipeline_instance
