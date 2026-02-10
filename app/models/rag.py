# app/models/rag.py
"""
RAG相关数据模型（用于请求/响应验证）
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from datetime import datetime


@dataclass
class IntentRequest:
    """意图判断请求模型"""
    query: str
    current_date: Optional[str] = None  # YYYY-MM-DD格式，不提供则自动获取
    
    def __post_init__(self):
        if not self.query or not self.query.strip():
            raise ValueError("query不能为空")
        self.query = self.query.strip()
        if self.current_date is None:
            self.current_date = datetime.now().strftime("%Y-%m-%d")


@dataclass
class IntentResponse:
    """意图判断响应模型"""
    needs_search: bool
    intent_type: str
    category: str
    core_claim: str
    is_historical: bool = False
    time_window: str = ""
    resolved_date: str = ""
    scope: str = "both"
    key_metrics: str = ""
    error: Optional[str] = None
    
    def to_dict(self) -> Dict:
        return {
            "needs_search": self.needs_search,
            "intent_type": self.intent_type,
            "category": self.category,
            "core_claim": self.core_claim,
            "is_historical": self.is_historical,
            "time_window": self.time_window,
            "resolved_date": self.resolved_date,
            "scope": self.scope,
            "key_metrics": self.key_metrics,
            "error": self.error
        }


@dataclass
class QueryRequest:
    """RAG查询请求模型"""
    query: str
    top_k: int = 5
    filter_source: Optional[str] = None
    filter_category: Optional[str] = None
    filter_date_from: Optional[str] = None  # YYYY-MM-DD格式
    filter_date_to: Optional[str] = None    # YYYY-MM-DD格式
    
    def __post_init__(self):
        if not self.query or not self.query.strip():
            raise ValueError("query不能为空")
        self.query = self.query.strip()
        if self.top_k < 1:
            self.top_k = 1
        if self.top_k > 20:
            self.top_k = 20


@dataclass
class SourceItem:
    """来源项模型"""
    title: str
    source: str
    category: Optional[str]
    link: str
    score: float
    published_time: Optional[str]
    
    def to_dict(self) -> Dict:
        return {
            "title": self.title,
            "source": self.source,
            "category": self.category,
            "link": self.link,
            "score": self.score,
            "published_time": self.published_time
        }


@dataclass
class QueryResponse:
    """RAG查询响应模型"""
    answer: str
    sources: List[Dict] = field(default_factory=list)
    query_time: float = 0.0
    
    def to_dict(self) -> Dict:
        return {
            "answer": self.answer,
            "sources": self.sources,
            "query_time": self.query_time
        }
