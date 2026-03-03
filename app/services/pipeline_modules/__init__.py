# app/services/pipeline_modules/__init__.py
"""
Pipeline 子模块包
"""
from app.services.pipeline_modules.follow_up import (
    build_follow_up_temporal_context,                   # 构建追问的「今天/昨天」时间上下文供检索与回复
    classify_follow_up_type,                            # 判断是否为追问及类型（时间切换/延续/对象切换）
    _CATEGORY_KEYWORDS_INFER,                           # 推断对话类型的分类关键词表
    _CTX_DATE_PATTERNS,                                 # 解析「今天/昨天」等时间上下文的日期正则
    _EVENT_CONTINUE_PATTERN,                            # 识别「再/继续/详细」等延续追问的正则
    _get_last_turn_category,                            # 从历史取上一轮对话分类供路由与漂移检测
    _infer_category_from_text,                          # 从单句文本用关键词推断 filter_category
    _parse_assistant_event_time,                        # 从助理事件解析时间用于时间继承
    _parse_last_user_time,                              # 从用户上条消息解析时间用于时间继承
)
from app.services.pipeline_modules.search_helpers import (
    _filter_by_semantic_score,                          # 按语义分阈值过滤检索结果
    _filter_published_on_date,                          # 按发布日期筛选检索结果（报道模式降级）
    _get_retrieval_min_semantic_score,                  # 读取配置的检索最低语义分
    _get_retrieval_term_overlap_boost_weight,           # 读取配置的词重叠加分权重
    _inject_date_into_query_for_search,                 # 将「昨天/今天」等替换为具体日期供检索
    _term_overlap_ratio,                                # 计算查询与文档字符重叠比例用于加分
)
from app.services.pipeline_modules.scores_formatter import (
    _format_scores_reply,                               # 将赛况数据格式化为比分播报文本
    _read_nba_scores_for_query,                         # 按日期与 query 读 NBA 比分并筛选场次
)
from app.services.pipeline_modules.sse_utils import (
    _get_last_turn_user_input_from_history,             # 从历史取上一轮用户输入供改写
    _sanitize_event,                                    # 对 SSE 事件做安全显示过滤避免乱码
)

__all__ = [
    "build_follow_up_temporal_context",
    "classify_follow_up_type",
    "_CATEGORY_KEYWORDS_INFER",
    "_CTX_DATE_PATTERNS",
    "_EVENT_CONTINUE_PATTERN",
    "_filter_by_semantic_score",
    "_filter_published_on_date",
    "_format_scores_reply",
    "_get_last_turn_category",
    "_get_last_turn_user_input_from_history",
    "_get_retrieval_min_semantic_score",
    "_get_retrieval_term_overlap_boost_weight",
    "_infer_category_from_text",
    "_inject_date_into_query_for_search",
    "_parse_assistant_event_time",
    "_parse_last_user_time",
    "_read_nba_scores_for_query",
    "_sanitize_event",
    "_term_overlap_ratio",
]

