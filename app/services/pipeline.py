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
from app.services.router import Router, get_router
from app.services.session_state import SessionStateManager, get_session_state_manager
from app.services.pipeline_logger import PipelineLogger, get_pipeline_logger
from app.services.pipeline_tracer import PipelineTracer
from app.services.vector_store import make_dedup_key
from app.utils.text_encoding import safe_for_display


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
            self._llm_service = LLMService()
        return self._llm_service
    
    # ========== 搜索辅助 ==========

    # category fallback 时的质量阈值：低于此分数视为"等同于没搜到"
    _CATEGORY_FALLBACK_SCORE = 0.45

    # time_sensitivity -> 时间衰减半衰期（天）
    _TIME_DECAY_HALF_LIFE = {
        "realtime": 1,
        "recent": 7,
        "historical": 30,
        "none": 30,
    }

    @staticmethod
    def _apply_time_decay(
        results: List[Dict[str, Any]],
        anchor_date: datetime,
        half_life_days: float = 30.0,
    ) -> List[Dict[str, Any]]:
        """
        对搜索结果施加时间衰减加权，按发布日期与锚点日期的距离衰减分数。

        衰减公式: adjusted_score = semantic_score * half_life / (half_life + days_diff)
        - days_diff = 0  -> weight = 1.0
        - days_diff = half_life -> weight = 0.5
        - days_diff >> half_life -> weight -> 0

        Args:
            results: 搜索结果列表（会被原地修改）
            anchor_date: 锚点日期（用户提及的日期或请求发起日期）
            half_life_days: 半衰期天数
        Returns:
            按 adjusted score 降序排列的结果列表
        """
        anchor_naive = anchor_date.replace(tzinfo=None)

        for item in results:
            published_time = item.get("published_time")
            if not published_time:
                continue
            try:
                pub_dt = datetime.fromisoformat(published_time)
                pub_naive = pub_dt.replace(tzinfo=None)
                days_diff = abs((anchor_naive - pub_naive).total_seconds()) / 86400.0
                time_weight = half_life_days / (half_life_days + days_diff)
                item["original_score"] = item.get("score", 0)
                item["time_weight"] = round(time_weight, 4)
                item["score"] = round(item["original_score"] * time_weight, 4)
            except Exception as e:
                logger.debug(f"时间衰减计算跳过: published_time={published_time}, err={e}")

        results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return results

    def _resolve_time_decay_params(
        self,
        search_params: Dict[str, Any],
        current_date_str: Optional[str],
    ) -> tuple:
        """
        从 search_params 中解析出时间衰减所需的 anchor_date 和 half_life_days。

        Returns:
            (anchor_date: datetime, half_life_days: float)
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
        half_life_days = self._TIME_DECAY_HALF_LIFE.get(time_sensitivity, 30)

        return anchor_date, half_life_days

    def _search_with_category_fallback(
        self,
        search_queries: List[str],
        standalone_query: str,
        top_k: int,
        filter_source: Optional[str],
        filter_category: Optional[str],
        filter_date_from: Optional[str],
        filter_date_to: Optional[str],
    ) -> List[Dict[str, Any]]:
        """
        带 category 自动扩增的搜索。
        
        流程：
        1. 用指定 filter_category 搜索
        2. 若结果为空或最高分 < 阈值 → 去掉 category 过滤，全库搜索
        3. 合并去重，返回 top_k
        """
        fallback_query = standalone_query.replace("最新", "").replace("动态", "").replace("情况", "").strip()

        # 第一轮：带 category 搜索
        results = self.vector_store.search_with_expansion(
            queries=search_queries,
            top_k=top_k,
            filter_source=filter_source,
            filter_category=filter_category,
            filter_date_from=filter_date_from,
            filter_date_to=filter_date_to,
            fallback_query=fallback_query if fallback_query != standalone_query else None,
        )

        best_score = results[0].get("score", 0) if results else 0

        # 第二轮：category fallback（去掉 category 过滤，全库搜）
        if filter_category and (not results or best_score < self._CATEGORY_FALLBACK_SCORE):
            logger.info(
                f"Category fallback: filter_category={filter_category} 结果 {len(results)} 条, "
                f"best_score={best_score:.4f} < {self._CATEGORY_FALLBACK_SCORE} → 去掉 category 过滤重搜"
            )
            broader_results = self.vector_store.search_with_expansion(
                queries=search_queries,
                top_k=top_k,
                filter_source=filter_source,
                filter_category=None,  # 全库
                filter_date_from=filter_date_from,
                filter_date_to=filter_date_to,
                fallback_query=fallback_query if fallback_query != standalone_query else None,
            )
            # 合并两轮结果，去重保留最高分
            seen: Dict[str, Dict] = {}
            for item in results + broader_results:
                key = make_dedup_key(item)
                if key not in seen or item.get("score", 0) > seen[key].get("score", 0):
                    seen[key] = item
            results = sorted(seen.values(), key=lambda x: x.get("score", 0), reverse=True)[:top_k]
            logger.info(f"Category fallback 后: 共 {len(results)} 条")

        return results

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
            
            # 2. 预处理层：查询改写（两阶段：独立性判断 + LLM 改写）
            t0 = time.time()
            standalone_query = self.query_rewriter.rewrite(
                current_input=input_data.query,
                history=history,
            )
            latency.rewrite_ms = (time.time() - t0) * 1000
            rewrite_skipped = (standalone_query.strip() == input_data.query.strip())
            rewrite_prompt = ""
            if not rewrite_skipped and history:
                history_text = self.query_rewriter._format_history(history)
                rewrite_prompt = self.query_rewriter.REWRITE_PROMPT.format(
                    history=history_text,
                    current_input=input_data.query,
                )
            tracer.record_rewrite(
                prompt=rewrite_prompt,
                result=standalone_query,
                elapsed_ms=latency.rewrite_ms,
                skipped=rewrite_skipped,
            )
            
            # 3. 分类层：意图分类（规则前置 + 两阶段 LLM）
            t0 = time.time()
            current_date = input_data.current_date or datetime.now().strftime("%Y-%m-%d")
            classification = self.intent_classifier.classify(
                standalone_query=standalone_query,
                current_date=current_date
            )
            latency.classify_ms = (time.time() - t0) * 1000
            # trace: 如果分类耗时极短（<5ms）说明是规则命中，标注 "(规则前置)"
            if latency.classify_ms < 5:
                classify_prompt = f"(规则前置命中，未调用 LLM)\n查询: {standalone_query}"
            else:
                classify_prompt = self.intent_classifier.CLASSIFY_PROMPT.format(
                    current_date=current_date,
                    query=standalone_query,
                )
            tracer.record_classify(
                prompt=classify_prompt,
                result_dict=classification.model_dump(),
                elapsed_ms=latency.classify_ms,
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

            # ===== STEP 1: 查询改写（两阶段：独立性判断 + LLM 改写） =====
            t0 = time.time()
            standalone_query = self.query_rewriter.rewrite(
                current_input=input_data.query,
                history=history,
            )
            latency.rewrite_ms = (time.time() - t0) * 1000
            # -- trace: 改写 --
            rewrite_skipped = (standalone_query.strip() == input_data.query.strip())
            rewrite_prompt = ""
            if not rewrite_skipped and history:
                history_text = self.query_rewriter._format_history(history)
                rewrite_prompt = self.query_rewriter.REWRITE_PROMPT.format(
                    history=history_text,
                    current_input=input_data.query,
                )
            tracer.record_rewrite(
                prompt=rewrite_prompt,
                result=standalone_query,
                elapsed_ms=latency.rewrite_ms,
                skipped=rewrite_skipped,
            )

            # ===== STEP 2: 意图分类 (Qwen) =====
            t0 = time.time()
            current_date = input_data.current_date or datetime.now().strftime("%Y-%m-%d")
            classification = self.intent_classifier.classify(
                standalone_query=standalone_query,
                current_date=current_date
            )
            latency.classify_ms = (time.time() - t0) * 1000
            # -- trace: 分类 --
            if latency.classify_ms < 5:
                classify_prompt = f"(规则前置命中，未调用 LLM)\n查询: {standalone_query}"
            else:
                classify_prompt = self.intent_classifier.CLASSIFY_PROMPT.format(
                    current_date=current_date,
                    query=standalone_query,
                )
            tracer.record_classify(
                prompt=classify_prompt,
                result_dict=classification.model_dump(),
                elapsed_ms=latency.classify_ms,
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
            for event in self._execute_stream(
                route_decision=route_decision,
                standalone_query=standalone_query,
                original_query=input_data.query,
                input_data=input_data,
                history=history,
                tracer=tracer,
            ):
                if "choices" in event and event["choices"]:
                    delta = event["choices"][0].get("delta") or {}
                    content = delta.get("content")
                    if isinstance(content, str):
                        accumulated_answer += content
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
            # -- trace: GLM 输出（在 _execute_stream 已记录 prompt，此处补充最终回复） --
            tracer.record_glm_output(answer=accumulated_answer)
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
                    time_sensitivity="none", confidence=0.0
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
            )
        
        elif route_decision.action == "generate_direct":
            return self._execute_generate_direct(
                original_query=original_query,
                history=history,
                tracer=tracer,
            )
        
        elif route_decision.action in ("tool_quote", "tool_weather"):
            # 工具调用（当前降级为直接生成）
            logger.info(f"工具调用未实现，降级为直接生成: {route_decision.action}")
            return self._execute_generate_direct(
                original_query=original_query,
                history=history,
                tracer=tracer,
            )
        
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
    ) -> Iterator[Dict[str, Any]]:
        """执行层流式版本，yield SSE 事件 dict（content / replace / done）。"""
        if route_decision.action == "search_then_generate":
            search_params = route_decision.search_params or {}
            filter_category = input_data.filter_category or search_params.get("filter_category")
            filter_source = input_data.filter_source or search_params.get("filter_source")
            filter_date_from = input_data.filter_date_from or search_params.get("filter_date_from")
            filter_date_to = input_data.filter_date_to or search_params.get("filter_date_to")
            # 查询扩展：生成多个检索变体
            search_queries = self.llm_service.expand_queries_for_search(standalone_query, num_variants=3)
            logger.info(f"检索查询(扩展): {search_queries}")
            # 带 category fallback 的搜索
            search_results = self._search_with_category_fallback(
                search_queries=search_queries,
                standalone_query=standalone_query,
                top_k=input_data.top_k,
                filter_source=filter_source,
                filter_category=filter_category,
                filter_date_from=filter_date_from,
                filter_date_to=filter_date_to,
            )
            # 时间衰减重排
            anchor_date, half_life_days = self._resolve_time_decay_params(
                search_params, input_data.current_date
            )
            search_results = self._apply_time_decay(
                search_results, anchor_date, half_life_days
            )
            logger.info(
                f"时间衰减重排: anchor={anchor_date.strftime('%Y-%m-%d')}, "
                f"half_life={half_life_days}d"
            )
            logger.info(f"RAG 搜索结果(流式): 共 {len(search_results)} 条")
            for i, item in enumerate(search_results, 1):
                orig = item.get('original_score')
                tw = item.get('time_weight')
                extra = f" (orig={orig:.4f}, tw={tw:.4f})" if orig is not None else ""
                logger.info(
                    f"  [{i}] score={item.get('score', 0):.4f}{extra} | {item.get('published_time', '')} | {item.get('source', '')} | {item.get('title', '')[:60]}"
                )
            # -- trace: 搜索结果（含完整正文）--
            if tracer:
                tracer.record_search(
                    search_queries=search_queries,
                    results=search_results,
                    anchor_date=anchor_date,
                    half_life_days=half_life_days,
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
            if tracer:
                from app.services.llm_service import LLMService as _LLM
                _sys_prompt = _LLM._build_news_system_prompt()
                _ctx_text = "\n\n".join(
                    f"{i}. {_LLM._format_news_item(item)}"
                    for i, item in enumerate(search_results, 1)
                )
                _original_user_text = _LLM._build_news_user_prompt(_ctx_text, original_query)
                _full_user = f"{_original_user_text}\n\nRead again:\n{_original_user_text}"
                tracer.record_glm_prompt(
                    system_prompt=_sys_prompt,
                    user_prompt=_full_user,
                )
            deep_think = getattr(input_data, 'deep_think', False)
            for event in self.llm_service.generate_answer_stream(
                query=original_query,
                context=search_results,
                deep_think=deep_think
            ):
                yield _sanitize_event(event)
            yield _sanitize_event({"sources": sources, "done": True})
        else:
            # generate_direct / tool fallback / unknown action -> 直接生成（带历史）
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
    ) -> tuple[str, List[Dict[str, Any]], int]:
        """执行检索后生成"""
        # 合并检索参数
        search_params = route_decision.search_params or {}
        
        # 优先使用输入参数中的过滤条件
        filter_category = input_data.filter_category or search_params.get("filter_category")
        filter_source = input_data.filter_source or search_params.get("filter_source")
        filter_date_from = input_data.filter_date_from or search_params.get("filter_date_from")
        filter_date_to = input_data.filter_date_to or search_params.get("filter_date_to")
        
        # 1. 查询扩展：生成多个检索变体
        search_queries = self.llm_service.expand_queries_for_search(standalone_query, num_variants=3)
        logger.info(f"检索查询(扩展): {search_queries}")
        
        # 2. 带 category fallback 的搜索
        search_results = self._search_with_category_fallback(
            search_queries=search_queries,
            standalone_query=standalone_query,
            top_k=input_data.top_k,
            filter_source=filter_source,
            filter_category=filter_category,
            filter_date_from=filter_date_from,
            filter_date_to=filter_date_to,
        )
        
        # 时间衰减重排
        anchor_date, half_life_days = self._resolve_time_decay_params(
            search_params, input_data.current_date
        )
        search_results = self._apply_time_decay(
            search_results, anchor_date, half_life_days
        )
        logger.info(
            f"时间衰减重排: anchor={anchor_date.strftime('%Y-%m-%d')}, "
            f"half_life={half_life_days}d"
        )
        
        # -- trace: 搜索结果 --
        if tracer:
            tracer.record_search(
                search_queries=search_queries,
                results=search_results,
                anchor_date=anchor_date,
                half_life_days=half_life_days,
            )
        
        if not search_results:
            return "抱歉，没有找到相关的新闻内容。", [], 0
        
        logger.info(f"RAG 搜索结果(同步): 共 {len(search_results)} 条")
        for i, item in enumerate(search_results, 1):
            orig = item.get('original_score')
            tw = item.get('time_weight')
            extra = f" (orig={orig:.4f}, tw={tw:.4f})" if orig is not None else ""
            logger.info(
                f"  [{i}] score={item.get('score', 0):.4f}{extra} | {item.get('published_time', '')} | {item.get('source', '')} | {item.get('title', '')[:60]}"
            )
        
        # -- trace: 重建 GLM 完整 prompt --
        if tracer:
            from app.services.llm_service import LLMService as _LLM
            _sys_prompt = _LLM._build_news_system_prompt()
            _ctx_text = "\n\n".join(
                f"{i}. {_LLM._format_news_item(item)}"
                for i, item in enumerate(search_results, 1)
            )
            _original_user_text = _LLM._build_news_user_prompt(_ctx_text, original_query)
            _full_user = f"{_original_user_text}\n\nRead again:\n{_original_user_text}"
            tracer.record_glm_prompt(
                system_prompt=_sys_prompt,
                user_prompt=_full_user,
            )
        
        # 3. LLM生成回答
        answer = self.llm_service.generate_answer(
            query=original_query,
            context=search_results
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
        standalone_query = self.query_rewriter.rewrite(
            current_input=query,
            history=history,
        )
        
        # 分类
        classification = self.intent_classifier.classify(
            standalone_query=standalone_query,
            current_date=current_date
        )
        
        # 转换为旧格式（filter_category 即检索类别）
        return {
            "needs_search": classification.needs_search,
            "intent_type": self._map_intent_type(classification.intent_type),
            "category": classification.filter_category,
            "filter_category": classification.filter_category,
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
