# app/services/schemas.py
"""
Pipeline 数据模型定义
使用 Pydantic 进行类型约束和验证
"""
from datetime import datetime
from typing import List, Dict, Optional, Literal, Any
from pydantic import BaseModel, Field, field_validator


# ============ 时间解析层输出 ============

TimeSource = Literal["query", "inherited", "last_user", "default"]

# 追问类型：用于时间继承决策
FollowUpType = Literal[
    "time_switch",      # 时间切换：用户显式表达新时间（如「那今天呢」），不继承
    "event_continue",   # 事件继续：用户要求更多（如「再详细点」），继承 assistant_event_time
    "object_switch",    # 对象切换：用户切换到新实体（如「勇士呢」），继承 assistant_event_time
]


class TemporalContext(BaseModel):
    """
    时间解析结果，由 TemporalResolver 或 TemporalContextBuilder 产出。
    分层建模：query_time（用户显式时间）与 event_time（助手回答中的事件日期）分离。
    """
    reference_date: Optional[str] = Field(
        default=None,
        description="最终生效的检索锚点日期（YYYY-MM-DD）"
    )
    date_range_from: Optional[str] = Field(
        default=None,
        description="检索用起始日期（含缓冲，覆盖前日报道次日事件）"
    )
    date_range_to: Optional[str] = Field(
        default=None,
        description="检索用结束日期（含缓冲，覆盖次日报道当日事件）"
    )
    resolved: bool = Field(
        default=False,
        description="是否成功解析出明确日期"
    )
    # 分层字段（用于 trace 与调试）
    query_time: Optional[str] = Field(
        default=None,
        description="从当前用户句解析的时间（用户显式表达）"
    )
    event_time: Optional[str] = Field(
        default=None,
        description="从 last_assistant 抽取的事件日期（assistant 文本中的日期）"
    )
    inherited_event_time: Optional[str] = Field(
        default=None,
        description="追问时继承自 assistant 的日期（与 event_time 相同，语义区分）"
    )
    time_source: Optional[TimeSource] = Field(
        default=None,
        description="reference_date 的来源：query=用户显式, inherited=继承助手, last_user=继承上轮用户, default=当前日"
    )


# ============ 时间意图层（独立于 ClassificationResult）============

TimeReferenceType = Literal["publish_time", "event_time", "ambiguous"]


class TimeIntent(BaseModel):
    """时间意图：用户问的「昨天」指昨日报道还是昨日发生的事"""
    time_reference_type: TimeReferenceType = Field(
        description="publish_time=昨日报道, event_time=昨日发生的事, ambiguous=按 event_time 处理"
    )


TimeFilterStrategy = Literal["publish_time_only", "event_time_with_fallback"]


# ============ 时间职责解耦：派生范围（由 temporal_scope 推导） ============
# AnswerScopeDate: 本问是否限定「只答某一天」；有值则注入日期约束并做时间对齐，None 则不限定。
# 类型约定为 Optional[str]（YYYY-MM-DD），由 compute_answer_scope_date() 产出。
AnswerScopeDate = Optional[str]

# AnswerScopeMode: 目标日约束的宽严策略，由 compute_answer_scope_mode() 在检索完成后根据 context 产出。
# strict_date  — 仅可播报事件发生日期等于 answer_scope_date 的新闻。
# report_day_ok — 无目标日事件但有目标日报道时，允许「未找到目标日当天…；根据目标日报道前几日有…」。
AnswerScopeMode = Literal["strict_date", "report_day_ok"]

# 检索时间范围与 search_params 时间部分一致，由 compute_retrieval_scope() 产出并合并进 search_params。


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


# 路由小 LLM 输出（仅用户句 + 上轮类别输入，用于 Router 决策）
# action 由 Router 根据 need_retrieval + need_scores 推导，不在此处枚举
TimeSensitivity = Literal["realtime", "recent", "historical", "none"]


# ============ Query Parser LLM 输出（结构化理解） ============

QueryEntityType = Literal[
    "team", "player", "league", "sport_type",
    "financial", "person", "org", "time", "location", "other",
]

QueryIntentType = Literal[
    "scores",         # 比分/赛果/几比几
    "player_stats",   # 球员数据/详细统计
    "game_detail",    # 比赛细节/打得怎么样
    "news",           # 新闻/报道/动态
    "realtime_quote", # 实时行情/价格
    "general_query",  # 一般信息查询
    "chitchat",       # 闲聊/问候
]


class QueryEntity(BaseModel):
    """用户查询中提取的实体。"""
    type: QueryEntityType = Field(description="实体类型")
    value: str = Field(description="实体原文")


class QueryParseResult(BaseModel):
    """Parser LLM 输出：对用户查询的结构化理解，用于下游规则推导路由决策。"""
    entities: List[QueryEntity] = Field(default_factory=list, description="提取的实体列表")
    intent: QueryIntentType = Field(default="general_query", description="用户意图类型")
    category: FILTER_CATEGORY = Field(default="general", description="查询主类别")
    time_sensitivity: TimeSensitivity = Field(default="none", description="时效性")
    follow_up_type: Optional[FollowUpType] = Field(default=None, description="追问类型；非追问时为 None")

    @field_validator("follow_up_type", mode="before")
    @classmethod
    def _null_string_to_none(cls, v: Any) -> Any:
        """小模型常输出字符串 "null" 而非 JSON null。"""
        if v == "null" or v == "None":
            return None
        return v


def _action_from_intent(need_retrieval: bool, need_scores: bool) -> str:
    """由显式意图推导路由动作：scores_only -> tool_scores；否则 need_retrieval -> search_then_generate；否则 generate_direct。"""
    if need_scores and not need_retrieval:
        return "tool_scores"
    if need_retrieval:
        return "search_then_generate"
    return "generate_direct"


class RouteLLMOutput(BaseModel):
    """路由小 LLM 单次调用输出，显式意图维度，供 Router 推导 action 并编排。"""
    need_retrieval: bool = Field(
        default=False,
        description="是否需要检索（向量库/新闻）；true 时走 search_then_generate"
    )
    need_scores: bool = Field(
        default=False,
        description="是否需要赛况数据引擎（比分/赛果）；与 need_retrieval 独立，可单独或与检索混合"
    )
    filter_category: FILTER_CATEGORY = Field(
        default="general",
        description="检索主类别，与 FILTER_CATEGORY 一致"
    )
    time_sensitivity: TimeSensitivity = Field(
        default="none",
        description="时效性: realtime=实时, recent=近期, historical=历史, none=无"
    )
    follow_up_time_type: Optional[FollowUpType] = Field(
        default=None,
        description="追问时时间继承类型：time_switch=不继承(用户说了新时间), event_continue=继承(延续同一事件), object_switch=继承(换对象)；非追问或新话题为 None"
    )

    @field_validator("time_sensitivity", mode="before")
    @classmethod
    def _normalize_time_sensitivity(cls, v: Any) -> str:
        """LLM 可能输出 past，与 historical 同义，规范为 historical。"""
        if v == "past":
            return "historical"
        return v


def classification_from_route_output(route: RouteLLMOutput) -> "ClassificationResult":
    """由 RouteLLMOutput 构造 ClassificationResult；action 由 Router 根据 need_retrieval/need_scores 推导。"""
    action = _action_from_intent(route.need_retrieval, route.need_scores)
    intent_type = "tool_scores" if action == "tool_scores" else ("news" if route.need_retrieval else "knowledge")
    return ClassificationResult(
        needs_search=route.need_retrieval,
        need_retrieval=route.need_retrieval,
        need_scores=route.need_scores,
        intent_type=intent_type,
        filter_category=route.filter_category,
        filter_categories=[route.filter_category],
        time_sensitivity=route.time_sensitivity,
        confidence=0.9,
        reference_datetime=None,
    )


class ClassificationResult(BaseModel):
    """意图分类结果；显式意图维度 need_retrieval / need_scores，编排层据此决定检索与赛况是否混合。"""
    needs_search: bool = Field(
        description="是否需要检索向量库/新闻（与 need_retrieval 一致，兼容旧字段）"
    )
    need_retrieval: bool = Field(
        default=False,
        description="是否需要检索；true 时执行向量检索"
    )
    need_scores: bool = Field(
        default=False,
        description="是否需要赛况数据引擎；true 且 need_retrieval 时与检索混合注入 context，true 且非 need_retrieval 时仅赛况(scores_only)"
    )
    intent_type: Literal["news", "realtime_quote", "knowledge", "chitchat", "tool", "tool_scores"] = Field(
        description="意图类型: news=新闻查询, realtime_quote=实时行情, knowledge=常识问答, chitchat=闲聊, tool=工具调用, tool_scores=NBA赛况数据引擎"
    )
    filter_category: FILTER_CATEGORY = Field(
        default="general",
        description="检索主类别（即 filter_categories 的第一项，兼容旧逻辑）"
    )
    filter_categories: List[FILTER_CATEGORY] = Field(
        default_factory=list,
        description="检索类别 top-k（最多 3 个），LLM 按相关度排序；检索时在此列表中匹配"
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
    """
    会话状态，用于路由与防护。
    注意：用户可能删上数轮 Q&A，故「上轮类别」等应以当前请求的 history 推断为准，不依赖本结构。
    """
    conversation_id: str = Field(
        description="对话ID"
    )
    last_filter_category: Optional[str] = Field(
        default=None,
        description="[已弃用，与删 Q&A 不兼容] 上轮类别请用 pipeline 从 history 推断的 effective_last_category"
    )
    last_entities: List[str] = Field(
        default_factory=list,
        description="上一轮提取的实体"
    )
    search_count: int = Field(
        default=0,
        description="服务端连续检索次数（防循环）；用户删上数轮 Q&A 后与用户可见轮次可能不一致"
    )
    last_route: Optional[str] = Field(
        default=None,
        description="上一轮路由（调试用）"
    )
    turn_count: int = Field(
        default=0,
        description="服务端处理轮次（如驱逐缓存用）；用户删 Q&A 后与用户可见轮次可能不一致"
    )


# ============ 路由决策 ============

class RouteDecision(BaseModel):
    """路由决策结果"""
    action: Literal[
        "search_then_generate",  # 先检索后生成
        "generate_direct",       # 直接生成
        "tool_quote",            # 调用行情工具
        "tool_weather",          # 调用天气工具
        "tool_scores",           # 调用赛况数据引擎
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
    total_ms:    float = Field(default=0.0)
    rewrite_ms:  float = Field(default=0.0)
    classify_ms: float = Field(default=0.0)
    route_ms:    float = Field(default=0.0)
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
    filter_source:    Optional[str] = Field(default=None)
    filter_category:  Optional[str] = Field(default=None)
    filter_date_from: Optional[str] = Field(default=None)
    filter_date_to:   Optional[str] = Field(default=None)


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


# ============ 改写层输出 ============


class RewriteResult(BaseModel):
    """查询改写结果（含可选理由，供生成层与 tracer 使用）"""
    standalone_query: str = Field(description="改写后的独立查询")
    reasoning: Optional[str] = Field(default=None, description="改写原因（若有实质性改写）")


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
