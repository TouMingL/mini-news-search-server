# app/services/__init__.py
# 服务层初始化模块
"""
RAG服务层模块，提供：

核心服务：
- EmbeddingService: 文本向量化服务
- VectorStore: 向量数据库操作服务
- LLMService: LLM调用服务（GLM-4-Flash）
- RAGService: RAG查询服务

Pipeline组件（新架构）：
- LocalLLMService: 本地LLM推理服务（Qwen2.5-3B）
- QueryRewriter: 查询改写器（预处理层）
- IntentClassifier: 意图分类器（分类层）
- Router: 路由决策器（决策层）
- SessionStateManager: 会话状态管理器
- Pipeline: Pipeline编排器
- PipelineLogger: Pipeline日志记录器

数据模型：
- schemas: Pydantic数据模型定义

[已废弃] IntentService: 旧版意图判断服务（保留用于向后兼容）
"""

# 核心服务
from app.services.embedding_service import EmbeddingService
from app.services.vector_store import VectorStore
from app.services.llm_service import LLMService
from app.services.rag_service import RAGService

# Pipeline组件
from app.services.local_llm_service import LocalLLMService, get_local_llm_service
from app.services.query_rewriter import QueryRewriter, get_query_rewriter
from app.services.intent_classifier import IntentClassifier, get_intent_classifier
from app.services.router import Router, get_router
from app.services.session_state import SessionStateManager, get_session_state_manager
from app.services.pipeline import Pipeline, get_pipeline
from app.services.pipeline_logger import PipelineLogger, get_pipeline_logger

# 数据模型
from app.services.schemas import (
    ClassificationResult,
    SessionState,
    RouteDecision,
    PipelineLog,
    PipelineInput,
    PipelineOutput,
    HistoryMessage,
    LatencyMetrics
)

# 旧版服务（已废弃，保留用于向后兼容）
from app.services.intent_service import IntentService

__all__ = [
    # 核心服务
    'EmbeddingService',
    'VectorStore',
    'LLMService',
    'RAGService',
    
    # Pipeline组件
    'LocalLLMService',
    'get_local_llm_service',
    'QueryRewriter',
    'get_query_rewriter',
    'IntentClassifier',
    'get_intent_classifier',
    'Router',
    'get_router',
    'SessionStateManager',
    'get_session_state_manager',
    'Pipeline',
    'get_pipeline',
    'PipelineLogger',
    'get_pipeline_logger',
    
    # 数据模型
    'ClassificationResult',
    'SessionState',
    'RouteDecision',
    'PipelineLog',
    'PipelineInput',
    'PipelineOutput',
    'HistoryMessage',
    'LatencyMetrics',
    
    # 已废弃
    'IntentService',
]
