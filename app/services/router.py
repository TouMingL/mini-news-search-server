# app/services/router.py
"""
决策层 - Router / Orchestrator
职责：结合分类结果 + 会话状态，决定路由（FSM 状态机模式）
路由决策的详细日志仅写入 miniprogram-server/logs 下按天共用的 log 文件。
"""
import json
import logging
from datetime import date
from pathlib import Path
from typing import Optional, Dict, Any
from loguru import logger

from app.services.schemas import (
    ClassificationResult,
    SessionState,
    RouteDecision
)
from app.services.session_state import SessionStateManager, get_session_state_manager


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


# 经济/财经类（用于实时行情规则）
ECONOMY_FILTER_CATEGORY = "economy"


class Router:
    """
    路由决策器
    基于 FSM（有限状态机）模式，结合分类结果和会话状态进行路由决策
    """
    
    def __init__(self, state_manager: SessionStateManager = None):
        """
        初始化 Router
        
        Args:
            state_manager: 会话状态管理器
        """
        self._state_manager = state_manager
    
    @property
    def state_manager(self) -> SessionStateManager:
        """延迟获取状态管理器"""
        if self._state_manager is None:
            self._state_manager = get_session_state_manager()
        return self._state_manager
    
    def decide(
        self,
        classification: ClassificationResult,
        state: SessionState,
        standalone_query: str
    ) -> RouteDecision:
        """
        根据分类结果和会话状态做出路由决策
        
        Args:
            classification: 分类结果
            state: 当前会话状态
            standalone_query: 独立查询（用于构建检索参数）
            
        Returns:
            RouteDecision 路由决策
        """
        _router_log = _router_file_logger()
        _router_log.info(
            "======== 路由决策 输入 ========\n%s",
            json.dumps({
                "classification": classification.model_dump(),
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

        # 检测上下文漂移
        if self.state_manager.detect_context_drift(classification, state):
            logger.info("检测到上下文漂移，重置连续搜索计数")
            state.search_count = 0

        # 检测搜索循环
        if self.state_manager.is_search_loop(state):
            logger.warning(f"检测到搜索循环（连续{state.search_count}次），强制直接生成")
            return _log_and_return(RouteDecision(
                action="generate_direct",
                reason=f"搜索循环保护：连续搜索{state.search_count}次",
                inherited_from_state=True
            ))

        # 是否继承上一轮 filter_category
        effective_filter_category = classification.filter_category
        if self.state_manager.should_inherit_category(classification, state):
            effective_filter_category = state.last_filter_category
            logger.info(f"继承上轮 filter_category: {state.last_filter_category}")

        # ========== FSM 路由规则 ==========

        # 规则1：闲聊 -> 直接生成
        if classification.intent_type == "chitchat":
            return _log_and_return(RouteDecision(
                action="generate_direct",
                reason="闲聊类型，直接生成回复"
            ))

        # 规则2：常识/知识 -> 直接生成
        if classification.intent_type == "knowledge" and not classification.needs_search:
            return _log_and_return(RouteDecision(
                action="generate_direct",
                reason="常识问答，无需检索"
            ))

        # 规则3：需要搜索 -> 向量检索后生成
        if classification.needs_search:
            search_params = self._build_search_params(
                classification=classification,
                effective_filter_category=effective_filter_category,
                standalone_query=standalone_query
            )
            return _log_and_return(RouteDecision(
                action="search_then_generate",
                search_params=search_params,
                reason=f"需要检索: intent={classification.intent_type}, filter_category={effective_filter_category}"
            ))

        # 规则4：实时行情 + 经济类 -> 可扩展工具调用（当前降级为搜索）
        if (classification.time_sensitivity == "realtime" and
            effective_filter_category == ECONOMY_FILTER_CATEGORY):
            search_params = self._build_search_params(
                classification=classification,
                effective_filter_category=effective_filter_category,
                standalone_query=standalone_query
            )
            return _log_and_return(RouteDecision(
                action="search_then_generate",
                search_params=search_params,
                reason=f"实时行情查询（降级为搜索）: {effective_filter_category}"
            ))

        # 规则5：工具调用（天气等）-> 当前降级为直接生成
        if classification.intent_type == "tool":
            return _log_and_return(RouteDecision(
                action="generate_direct",
                tool_name="weather",
                reason="天气查询（工具未实现，降级为直接生成）"
            ))

        # 规则6：Fallback -> 根据时效性决定
        if classification.time_sensitivity in ("realtime", "recent"):
            search_params = self._build_search_params(
                classification=classification,
                effective_filter_category=effective_filter_category,
                standalone_query=standalone_query
            )
            return _log_and_return(RouteDecision(
                action="search_then_generate",
                search_params=search_params,
                reason=f"Fallback: 有时效性要求，执行检索 filter_category={effective_filter_category}"
            ))

        # 默认：直接生成
        return _log_and_return(RouteDecision(
            action="generate_direct",
            reason="Fallback: 无明确检索需求"
        ))
    
    def _build_search_params(
        self,
        classification: ClassificationResult,
        effective_filter_category: str,
        standalone_query: str
    ) -> Dict[str, Any]:
        """
        构建检索参数
        
        filter_category 由 IntentClassifier 直接输出，无需映射
        """
        params: Dict[str, Any] = {
            "query": standalone_query,
            "filter_category": effective_filter_category
        }
        
        # 时效性推断日期范围
        from datetime import datetime, timedelta
        today = datetime.now()
        
        if classification.time_sensitivity == "realtime":
            # 实时：只查今天
            params["filter_date_from"] = today.strftime("%Y-%m-%d")
        elif classification.time_sensitivity == "recent":
            # 近期：最近7天
            params["filter_date_from"] = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        elif classification.time_sensitivity == "historical":
            # 历史：最近30天（可根据具体日期调整）
            params["filter_date_from"] = (today - timedelta(days=30)).strftime("%Y-%m-%d")
        # none: 不限制日期
        
        # 传递时间衰减所需参数
        params["time_sensitivity"] = classification.time_sensitivity
        params["reference_datetime"] = classification.reference_datetime
        
        return params
    
    def route_and_update_state(
        self,
        classification: ClassificationResult,
        conversation_id: str,
        standalone_query: str
    ) -> tuple[RouteDecision, SessionState]:
        """
        路由决策并更新会话状态（便捷方法）
        
        Args:
            classification: 分类结果
            conversation_id: 对话ID
            standalone_query: 独立查询
            
        Returns:
            (RouteDecision, 更新后的SessionState)
        """
        # 获取状态
        state = self.state_manager.get_state(conversation_id)
        
        # 做出决策
        decision = self.decide(classification, state, standalone_query)
        
        # 更新状态
        updated_state = self.state_manager.update_state(
            conversation_id=conversation_id,
            classification=classification,
            standalone_query=standalone_query,
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
