# app/services/__init__.py
# 服务层初始化模块
"""
RAG 服务层：

核心服务：EmbeddingService, VectorStore, LLMService, RAGService
Pipeline：LocalLLMService, QueryRewriter, IntentClassifier, Router, SessionStateManager, Pipeline, PipelineLogger
数据模型：schemas（Pydantic）
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
]
