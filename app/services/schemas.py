# app/services/schemas.py
"""
Pipeline 数据模型定义
使用 Pydantic 进行类型约束和验证
"""
from datetime import datetime
from typing import List, Dict, Optional, Literal, Any
from pydantic import BaseModel, Field


# ============ 分类层输出 ============

# 向量库 filter_category 枚举（与 Qdrant payload 一致，由 IntentClassifier 直接输出）
FILTER_CATEGORY = Literal[
    "academic",   # 学术
    "world",      # 国际新闻（中美、俄乌、地缘政治）
    "tech",       # 科技/数码
    "economy",    # 经济/财经（宏观、股市、外汇、贵金属、能源等）
    "sports",     # 体育（足球、篮球、奥运、羽毛球等）
    "general",    # 其他/未分类
    "health"      # 健康/医药
]


class ClassificationResult(BaseModel):
    """意图分类结果"""
    needs_search: bool = Field(
        description="是否需要检索向量库/新闻"
    )
    intent_type: Literal["news", "realtime_quote", "knowledge", "chitchat", "tool"] = Field(
        description="意图类型: news=新闻查询, realtime_quote=实时行情, knowledge=常识问答, chitchat=闲聊, tool=工具调用"
    )
    filter_category: FILTER_CATEGORY = Field(
        default="general",
        description="检索过滤类别，直接对应向量库 category，由 LLM 根据查询内容判断"
    )
    time_sensitivity: Literal["realtime", "recent", "historical", "none"] = Field(
        default="none",
        description="时效性: realtime=实时, recent=近期, historical=历史, none=无时效要求"
    )
    confidence: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="分类置信度 0-1"
    )
    reference_datetime: Optional[str] = Field(
        default=None,
        description="用户在查询中提及的日期/时间，解析为 YYYY-MM-DD 格式；用户未提及时为 None"
    )


# ============ 会话状态 ============

class SessionState(BaseModel):
    """会话状态，用于多轮对话的状态继承"""
    conversation_id: str = Field(
        description="对话ID"
    )
    last_filter_category: Optional[str] = Field(
        default=None,
        description="上一轮的 filter_category（用于继承）"
    )
    last_entities: List[str] = Field(
        default_factory=list,
        description="上一轮提取的实体"
    )
    last_standalone_query: Optional[str] = Field(
        default=None,
        description="上一轮的独立查询"
    )
    search_count: int = Field(
        default=0,
        description="连续搜索次数（防止无限循环）"
    )
    last_route: Optional[str] = Field(
        default=None,
        description="上一轮的路由决策"
    )
    turn_count: int = Field(
        default=0,
        description="对话轮次计数"
    )


# ============ 路由决策 ============

class RouteDecision(BaseModel):
    """路由决策结果"""
    action: Literal[
        "search_then_generate",  # 先检索后生成
        "generate_direct",       # 直接生成（不检索）
        "tool_quote",            # 调用行情工具
        "tool_weather",          # 调用天气工具
        "fallback"               # 兜底处理
    ] = Field(
        description="路由动作"
    )
    search_params: Optional[Dict[str, Any]] = Field(
        default=None,
        description="检索参数（当 action=search_then_generate 时使用）"
    )
    tool_name: Optional[str] = Field(
        default=None,
        description="工具名称（当 action=tool_* 时使用）"
    )
    tool_params: Optional[Dict[str, Any]] = Field(
        default=None,
        description="工具参数"
    )
    reason: str = Field(
        default="",
        description="路由决策原因（用于日志）"
    )
    inherited_from_state: bool = Field(
        default=False,
        description="是否从会话状态继承"
    )


# ============ Pipeline 日志 ============

class LatencyMetrics(BaseModel):
    """各阶段耗时指标"""
    total_ms: float = Field(default=0.0)
    rewrite_ms: float = Field(default=0.0)
    classify_ms: float = Field(default=0.0)
    route_ms: float = Field(default=0.0)
    retrieve_ms: float = Field(default=0.0)
    generate_ms: float = Field(default=0.0)


class PipelineLog(BaseModel):
    """Pipeline 全流程日志"""
    request_id: str = Field(
        description="请求唯一ID"
    )
    conversation_id: Optional[str] = Field(
        default=None,
        description="对话ID"
    )
    raw_input: str = Field(
        description="原始用户输入"
    )
    standalone_query: str = Field(
        description="改写后的独立查询"
    )
    classification: Dict[str, Any] = Field(
        default_factory=dict,
        description="分类结果"
    )
    route_decision: str = Field(
        description="路由决策"
    )
    retrieval_count: int = Field(
        default=0,
        description="检索结果数量"
    )
    final_response: str = Field(
        default="",
        description="最终响应"
    )
    latency: LatencyMetrics = Field(
        default_factory=LatencyMetrics,
        description="各阶段耗时"
    )
    timestamp: datetime = Field(
        default_factory=datetime.now,
        description="日志时间戳"
    )
    error: Optional[str] = Field(
        default=None,
        description="错误信息（如有）"
    )


# ============ Pipeline 输入输出 ============

class PipelineInput(BaseModel):
    """Pipeline 输入"""
    query: str = Field(
        description="用户查询"
    )
    conversation_id: Optional[str] = Field(
        default=None,
        description="对话ID（用于获取历史）"
    )
    history_turns: int = Field(
        default=5,
        ge=0,
        le=10,
        description="获取最近N轮历史"
    )
    current_date: Optional[str] = Field(
        default=None,
        description="当前日期（YYYY-MM-DD），默认自动获取"
    )
    # 深度思考
    deep_think: bool = Field(
        default=False,
        description="是否启用深度思考（GLM thinking 参数）"
    )
    # 检索参数透传
    top_k: int = Field(default=5, ge=1, le=20)
    filter_source: Optional[str] = Field(default=None)
    filter_category: Optional[str] = Field(default=None)
    filter_date_from: Optional[str] = Field(default=None)
    filter_date_to: Optional[str] = Field(default=None)


class PipelineOutput(BaseModel):
    """Pipeline 输出"""
    answer: str = Field(
        description="最终回答"
    )
    sources: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="来源信息"
    )
    classification: ClassificationResult = Field(
        description="分类结果"
    )
    route_decision: RouteDecision = Field(
        description="路由决策"
    )
    standalone_query: str = Field(
        description="改写后的独立查询"
    )
    query_time: float = Field(
        description="总耗时（秒）"
    )


# ============ 对话历史消息 ============

class HistoryMessage(BaseModel):
    """对话历史消息"""
    role: Literal["user", "assistant"] = Field(
        description="消息角色"
    )
    content: str = Field(
        description="消息内容"
    )
    timestamp: Optional[int] = Field(
        default=None,
        description="时间戳（毫秒）"
    )
