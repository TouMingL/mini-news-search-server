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
        standalone_query: str,
        route_action: str
    ) -> SessionState:
        """
        更新会话状态
        
        Args:
            conversation_id: 对话ID
            classification: 本轮分类结果
            standalone_query: 本轮独立查询
            route_action: 本轮路由动作
            
        Returns:
            更新后的 SessionState
        """
        state = self.get_state(conversation_id)
        
        # 更新 filter_category
        state.last_filter_category = classification.filter_category
        
        # 更新独立查询
        state.last_standalone_query = standalone_query
        
        # 更新路由
        state.last_route = route_action
        
        # 更新搜索计数
        if route_action == "search_then_generate":
            state.search_count += 1
        else:
            state.search_count = 0  # 非搜索路由时重置
        
        # 更新轮次
        state.turn_count += 1
        
        logger.debug(
            f"更新会话状态: conversation={conversation_id}, "
            f"filter_category={state.last_filter_category}, turn={state.turn_count}, "
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
        state: SessionState
    ) -> bool:
        """
        判断是否应该继承上一轮的 filter_category
        
        当当前分类置信度低且上一轮有明确类别时继承
        """
        # 置信度阈值
        CONFIDENCE_THRESHOLD = 0.6
        
        if not state.last_filter_category:
            return False
        
        if current_classification.confidence < CONFIDENCE_THRESHOLD:
            return True
        
        if current_classification.filter_category == "general" and state.last_filter_category != "general":
            return True
        
        return False
    
    def detect_context_drift(
        self,
        current_classification: ClassificationResult,
        state: SessionState
    ) -> bool:
        """
        检测上下文漂移（主题突变）
        
        Returns:
            True 表示检测到漂移，需要重置状态
        """
        if not state.last_filter_category:
            return False
        
        # 类别变化且置信度高 -> 漂移
        if (current_classification.filter_category != state.last_filter_category and
            current_classification.filter_category != "general" and
            current_classification.confidence > 0.7):
            logger.info(
                f"检测到上下文漂移: {state.last_filter_category} -> {current_classification.filter_category}"
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
