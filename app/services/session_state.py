# app/services/session_state.py
"""
会话状态管理器
职责：管理多轮对话的状态继承，支持上下文漂移检测
"""
from typing import Dict, Optional, List
from loguru import logger
from datetime import datetime

from app.services.schemas import SessionState, ClassificationResult, HistoryMessage


class SessionStateManager:
    """
    会话状态管理器
    使用内存缓存存储会话状态（生产环境可替换为 Redis）
    """
    
    # 内存缓存
    _state_cache: Dict[str, SessionState] = {}
    
    # 状态过期时间（秒）
    STATE_TTL_SECONDS = 3600  # 1小时
    
    # 最大连续搜索次数（防止无限循环）
    MAX_SEARCH_COUNT = 5
    
    def __init__(self):
        pass
    
    def get_state(self, conversation_id: str) -> SessionState:
        """
        获取会话状态，不存在则创建新状态
        
        Args:
            conversation_id: 对话ID
            
        Returns:
            SessionState 实例
        """
        if conversation_id in self._state_cache:
            return self._state_cache[conversation_id]
        
        # 创建新状态
        state = SessionState(conversation_id=conversation_id)
        self._state_cache[conversation_id] = state
        logger.debug(f"创建新会话状态: {conversation_id}")
        return state
    
    def update_state(
        self,
        conversation_id: str,
        classification: ClassificationResult,
        route_action: str
    ) -> SessionState:
        """
        更新会话状态
        
        Args:
            conversation_id: 对话ID
            classification: 本轮分类结果
            route_action: 本轮路由动作
            
        Returns:
            更新后的 SessionState
        """
        state = self.get_state(conversation_id)
        
        # 仅更新与「服务端处理历史」相关的字段；上轮类别以 pipeline 从当前请求 history 推断为准，不写此处（与删 Q&A 兼容）
        state.last_route = route_action
        
        # 服务端连续检索计数（用户删上数轮 Q&A 后可能与用户可见轮次不一致）
        if route_action == "search_then_generate":
            state.search_count += 1
        else:
            state.search_count = 0  # 非搜索路由时重置
        
        # 更新轮次
        state.turn_count += 1
        
        logger.debug(
            f"更新会话状态: conversation={conversation_id}, "
            f"route={state.last_route}, turn={state.turn_count}, "
            f"search_count={state.search_count}"
        )
        
        return state
    
    def extract_entities_from_query(self, query: str) -> List[str]:
        """
        从查询中提取实体（简单实现，可扩展为NER）
        
        Args:
            query: 查询字符串
            
        Returns:
            实体列表
        """
        # 金融实体关键词
        entity_keywords = {
            '黄金': ['黄金', '金价', 'XAUUSD'],
            '白银': ['白银', '银价', 'XAGUSD'],
            '原油': ['原油', '油价', 'WTI', '布伦特'],
            '美元': ['美元', 'USD', '美金'],
            '人民币': ['人民币', 'CNY', 'RMB'],
            '上证指数': ['上证', '大盘', 'A股']
        }
        
        entities = []
        query_upper = query.upper()
        
        for entity, keywords in entity_keywords.items():
            for kw in keywords:
                if kw.upper() in query_upper:
                    entities.append(entity)
                    break
        
        return entities
    
    def should_inherit_category(
        self,
        current_classification: ClassificationResult,
        effective_last_category: Optional[str],
    ) -> bool:
        """
        判断是否应该继承上一轮的 filter_category。
        与删数轮 Q&A 兼容：上轮类别由调用方从当前请求 history 推断传入，不读 state。
        """
        CONFIDENCE_THRESHOLD = 0.6
        CONFIDENCE_FLOOR = 0.5

        if not effective_last_category:
            return False

        if current_classification.confidence < CONFIDENCE_FLOOR:
            return False

        if current_classification.confidence < CONFIDENCE_THRESHOLD:
            return True

        if (current_classification.filter_category == "general"
            and effective_last_category != "general"):
            return True

        return False

    def detect_context_drift(
        self,
        current_classification: ClassificationResult,
        effective_last_category: Optional[str],
    ) -> bool:
        """
        检测上下文漂移（主题突变）。
        与删数轮 Q&A 兼容：上轮类别由调用方从当前请求 history 推断传入，不读 state。
        """
        if not effective_last_category:
            return False

        if (current_classification.filter_category != effective_last_category
            and current_classification.filter_category != "general"
            and current_classification.confidence > 0.7):
            logger.info(
                "检测到上下文漂移: %s -> %s",
                effective_last_category,
                current_classification.filter_category,
            )
            return True

        return False
    
    def is_search_loop(self, state: SessionState) -> bool:
        """
        检测是否陷入搜索循环
        """
        return state.search_count >= self.MAX_SEARCH_COUNT
    
    def reset_state(self, conversation_id: str) -> SessionState:
        """
        重置会话状态
        """
        state = SessionState(conversation_id=conversation_id)
        self._state_cache[conversation_id] = state
        logger.info(f"重置会话状态: {conversation_id}")
        return state
    
    def clear_expired_states(self):
        """
        清理过期的会话状态（可定时调用）
        """
        # 简单实现：清理超过一定数量的状态
        MAX_CACHE_SIZE = 1000
        
        if len(self._state_cache) > MAX_CACHE_SIZE:
            # 保留最近的一半
            sorted_keys = sorted(
                self._state_cache.keys(),
                key=lambda k: self._state_cache[k].turn_count,
                reverse=True
            )
            keys_to_remove = sorted_keys[MAX_CACHE_SIZE // 2:]
            for k in keys_to_remove:
                del self._state_cache[k]
            logger.info(f"清理过期会话状态: {len(keys_to_remove)} 个")


# 工厂函数
_session_state_manager_instance: Optional[SessionStateManager] = None


def get_session_state_manager() -> SessionStateManager:
    """获取 SessionStateManager 单例"""
    global _session_state_manager_instance
    if _session_state_manager_instance is None:
        _session_state_manager_instance = SessionStateManager()
    return _session_state_manager_instance
