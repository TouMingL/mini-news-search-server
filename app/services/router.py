# app/services/router.py
"""
决策层 - Router / Orchestrator
职责：根据 RouteLLM 输出 + 会话状态，做保护逻辑与参数构建（无 FSM 分支）
路由决策的详细日志仅写入 miniprogram-server/logs 下按天共用的 log 文件。
"""
import json
import logging
from datetime import date
from pathlib import Path
from typing import Optional, Dict, Any, List
from loguru import logger

from app.services.schemas import (
    RouteLLMOutput,
    SessionState,
    RouteDecision,
    TemporalContext,
    TimeIntent,
    classification_from_route_output,
    _action_from_intent,
)
from app.services.session_state import SessionStateManager, get_session_state_manager
from app.services.temporal_scope import compute_retrieval_scope


# ---------- 路由层专用文件日志（仅写 logs 目录，按天共用，不输出到控制台） ----------
def _router_log_dir() -> Path:
    """日志目录：miniprogram-server/logs"""
    return Path(__file__).resolve().parent.parent.parent / "logs"


class _DailyRouterFileHandler(logging.FileHandler):
    """按天切换的 FileHandler，写入 logs/router_YYYY-MM-DD.log"""
    def __init__(self):
        self._log_dir = _router_log_dir()
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._current_date = date.today()
        path = self._log_dir / f"router_{self._current_date.isoformat()}.log"
        super().__init__(str(path), encoding="utf-8")

    def emit(self, record: logging.LogRecord):
        if date.today() != self._current_date:
            self.close()
            self._current_date = date.today()
            self.baseFilename = str(
                self._log_dir / f"router_{self._current_date.isoformat()}.log"
            )
            self.stream = self._open()
        super().emit(record)


def _router_file_logger() -> logging.Logger:
    """仅写入 logs 目录的 router 专用 logger，不输出到控制台"""
    name = "router_file"
    log = logging.getLogger(name)
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        h = _DailyRouterFileHandler()
        h.setLevel(logging.INFO)
        h.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        log.addHandler(h)
    except Exception:
        pass
    return log


def _is_meaningless_for_search(standalone_query: str) -> bool:
    """检索用查询无效（无/空/过短）时返回 True，避免用无意义串做扩展检索。"""
    if not standalone_query:
        return True
    s = standalone_query.strip()
    if not s or s in ("无", "无。", "无，"):
        return True
    if len(s) <= 1:
        return True
    return False


class Router:
    """
    路由决策器
    基于 RouteLLM 输出做保护（无意义查询、搜索循环）与 search_params 构建，无 FSM 分支
    """

    def __init__(self, state_manager: SessionStateManager = None):
        self._state_manager = state_manager

    @property
    def state_manager(self) -> SessionStateManager:
        if self._state_manager is None:
            self._state_manager = get_session_state_manager()
        return self._state_manager

    def decide(
        self,
        route_llm_output: RouteLLMOutput,
        state: SessionState,
        standalone_query: str,
        temporal_context: Optional[TemporalContext] = None,
        time_intent: Optional[TimeIntent] = None,
        effective_last_category: Optional[str] = None,
    ) -> RouteDecision:
        """
        根据 RouteLLM 输出和会话状态做出路由决策。
        由 need_retrieval / need_scores 推导 action；无意义查询与搜索循环时覆盖为 generate_direct。
        effective_last_category：上轮类别（由 pipeline 从当前请求 history 推断），用于上下文漂移判定，与删数轮 Q&A 兼容。
        """
        _router_log = _router_file_logger()
        _router_log.info(
            "======== 路由决策 输入 ========\n%s",
            json.dumps({
                "route_llm_output": route_llm_output.model_dump(),
                "state": state.model_dump(),
                "standalone_query": standalone_query,
            }, ensure_ascii=False, indent=2)
        )

        def _log_and_return(decision: RouteDecision) -> RouteDecision:
            _router_log.info(
                "======== 路由决策 输出 ========\naction=%s | reason=%s | search_params=%s | tool_name=%s\n%s",
                decision.action,
                decision.reason,
                decision.search_params,
                decision.tool_name,
                json.dumps(decision.model_dump(), ensure_ascii=False, indent=2),
            )
            return decision

        # 无意义查询：若要走检索但查询无效，改为直接生成（仅覆盖决策 action，不改 route 输出）
        need_retrieval = getattr(route_llm_output, "need_retrieval", False)
        need_scores    = getattr(route_llm_output, "need_scores", False)
        if need_retrieval and _is_meaningless_for_search(standalone_query):
            return _log_and_return(RouteDecision(
                action="generate_direct",
                reason="查询无效或无法理解，无法执行检索",
            ))

        action = _action_from_intent(need_retrieval, need_scores)

        # 上下文漂移：当前类别与「当前请求 history 推断的上轮类别」不同且非 general 时重置连续搜索计数（不依赖 state，与删数轮 Q&A 兼容）
        if (effective_last_category is not None
            and route_llm_output.filter_category != effective_last_category
            and route_llm_output.filter_category != "general"):
            logger.info("检测到上下文漂移，重置连续搜索计数")
            state.search_count = 0

        # 搜索循环保护：基于服务端连续检索次数，用户删上数轮 Q&A 后与用户可见轮次可能不一致
        if self.state_manager.is_search_loop(state):
            logger.warning("检测到搜索循环（连续%d次），强制直接生成", state.search_count)
            return _log_and_return(RouteDecision(
                action="generate_direct",
                reason=f"搜索循环保护：连续搜索{state.search_count}次",
                inherited_from_state=True
            ))

        effective_filter_category = route_llm_output.filter_category
        effective_filter_categories = [effective_filter_category]

        if action == "tool_scores":
            return _log_and_return(RouteDecision(
                action="tool_scores",
                tool_name="scores",
                reason="赛况数据引擎(scores_only)：仅读本地 JSON，不检索不生成"
            ))

        if action == "search_then_generate":
            search_params = self._build_search_params(
                route_llm_output=route_llm_output,
                effective_filter_category=effective_filter_category,
                effective_filter_categories=effective_filter_categories,
                standalone_query=standalone_query,
                temporal_context=temporal_context,
                time_intent=time_intent,
            )
            return _log_and_return(RouteDecision(
                action="search_then_generate",
                search_params=search_params,
                reason=f"RouteLLM: 检索 filter_category={effective_filter_category}"
            ))

        return _log_and_return(RouteDecision(
            action="generate_direct",
            reason="RouteLLM: 直接生成"
        ))

    def _build_search_params(
        self,
        route_llm_output: RouteLLMOutput,
        effective_filter_category: str,
        standalone_query: str,
        effective_filter_categories: Optional[List[str]] = None,
        temporal_context: Optional[TemporalContext] = None,
        time_intent: Optional[TimeIntent] = None,
    ) -> Dict[str, Any]:
        """根据 RouteLLM 输出与时间上下文构建检索参数。时间部分由 temporal_scope 统一推导。"""
        params: Dict[str, Any] = {
            "query": standalone_query,
            "filter_category": effective_filter_category
        }
        if effective_filter_categories:
            params["filter_categories"] = effective_filter_categories[:3]

        follow_up_type = getattr(route_llm_output, "follow_up_time_type", None)
        retrieval_scope = compute_retrieval_scope(
            temporal_context=temporal_context,
            follow_up_type=follow_up_type,
            time_intent=time_intent,
            time_sensitivity=route_llm_output.time_sensitivity,
        )
        params.update(retrieval_scope)
        return params

    def route_and_update_state(
        self,
        route_llm_output:        RouteLLMOutput,
        route_decision:          RouteDecision,
        conversation_id:         str,
        standalone_query:        str,
        temporal_context:        Optional[TemporalContext] = None,
        time_intent:             Optional[TimeIntent] = None,
        effective_last_category: Optional[str] = None,
    ) -> tuple[RouteDecision, SessionState]:
        """
        一次完成：取 state -> decide -> 用 RouteLLM 输出更新 state。
        与 decide() 一致：effective_last_category 由调用方从当前请求 history 推断传入，用于上下文漂移重置 search_count；未传则视为无上轮类别。
        """
        state    = self.state_manager.get_state(conversation_id)
        decision = self.decide(
            route_llm_output,
            state,
            standalone_query,
            temporal_context=temporal_context,
            time_intent=time_intent,
            effective_last_category=effective_last_category,
        )
        classification = classification_from_route_output(route_llm_output)
        updated_state  = self.state_manager.update_state(
            conversation_id=conversation_id,
            classification=classification,
            route_action=decision.action
        )
        return decision, updated_state


# 工厂函数
_router_instance: Optional[Router] = None


def get_router() -> Router:
    """获取 Router 单例"""
    global _router_instance
    if _router_instance is None:
        _router_instance = Router()
    return _router_instance
