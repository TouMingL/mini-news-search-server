# app/services/pipeline_modules/follow_up.py
"""
追问类型识别与时间继承编排。

产品场景：用户在多轮对话中追问「勇士呢」「再详细点」「那昨天呢」时，
自动识别追问类型并从上下文继承/切换时间锚点，保证检索与回答的时间一致性。
"""
import re
from datetime import datetime, timedelta
from typing import List, Optional

from app.services.schemas import (
    TemporalContext,
    HistoryMessage,
    FollowUpType,
)
from app.services.temporal_resolver import TemporalResolver


# 从单句文本推断 filter_category（仅当 state 无上轮类别时，用历史最后一条用户句补全）
_CATEGORY_KEYWORDS_INFER = [
    ("sports", ["打得", "比赛", "nba", "cba", "火箭", "湖人", "马刺", "76人", "热火", "比分", "赛果", "体育", "篮球", "足球"]),
    ("economy", ["黄金", "白银", "油价", "股票", "股市", "汇率", "价格", "多少钱", "财经", "金融"]),
    ("tech", ["芯片", "手机", "苹果", "数码", "科技", "ai", "大模型"]),
    ("world", ["中美", "俄乌", "国际", "政治"]),
    ("health", ["健康", "医药", "疫苗"]),
]


def _infer_category_from_text(text: str) -> Optional[str]:
    """从单句文本用关键词推断 filter_category，用于 state 无上轮类别时从历史最后一句补全。"""
    if not text or not text.strip():
        return None
    lower = text.strip().lower()
    for category, keywords in _CATEGORY_KEYWORDS_INFER:
        if any(kw in lower for kw in keywords):
            return category
    return None


def _get_last_turn_category(
    history: Optional[List[HistoryMessage]],
) -> Optional[str]:
    """
    供 RouteLLM / 漂移检测使用的「上轮类别」：仅从当前请求的 history 推断。
    与删数轮 Q&A 兼容——用户删掉上数轮后 history 变短，上轮类别自然对齐。
    """
    if not history:
        return None
    for i in range(len(history) - 1, -1, -1):
        msg = history[i]
        if getattr(msg, "role", None) == "user":
            content = getattr(msg, "content", "") or ""
            inferred = _infer_category_from_text(content)
            if inferred:
                return inferred
            break
    return None


# 追问类型识别（纯规则，不调用 LLM）
_EVENT_CONTINUE_PATTERN = re.compile(
    r"再|继续|详细|具体|有吗|更多|还有|补充|说说"
)


def classify_follow_up_type(
    current_query: str,
    query_temporal_resolved: bool,
    last_turn_category: Optional[str],
) -> Optional[FollowUpType]:
    """
    追问类型识别，用于时间继承决策。
    仅当 last_turn_category 时（确认为追问）才返回类型；非追问返回 None。

    规则：
    - time_switch: 当前句含时间表达（用户显式指定新时间），不继承
    - event_continue: 当前句无时间且匹配「再/继续/详细/有吗」等延续模式，继承
    - object_switch: 当前句无时间且不匹配 event_continue（如「勇士呢」），继承
    """
    if not last_turn_category:
        return None
    if query_temporal_resolved:
        return "time_switch"
    q = (current_query or "").strip()
    if not q:
        return "event_continue"
    if _EVENT_CONTINUE_PATTERN.search(q):
        return "event_continue"
    return "object_switch"


# 追问时从历史推断 reference_date，按职责拆分为独立解析函数与编排层
_CTX_DATE_PATTERNS = [
    re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日"),
    re.compile(r"(\d{4})-(\d{2})-(\d{2})"),
    re.compile(r"(\d{1,2})月(\d{1,2})日"),
]


def _parse_assistant_event_time(
    history: List[HistoryMessage],
    ref_year: int,
) -> Optional[str]:
    """
    职责：从 last_assistant 文本中抽取第一个可识别的日期（事件日期）。
    返回 YYYY-MM-DD 或 None。不返回 TemporalContext，仅做提取。
    """
    for i in range(len(history) - 1, -1, -1):
        msg = history[i]
        if getattr(msg, "role", None) != "assistant":
            continue
        content = (getattr(msg, "content", "") or "").strip()
        if not content:
            continue
        year = ref_year
        for pat in _CTX_DATE_PATTERNS:
            m = pat.search(content)
            if not m:
                continue
            g = m.groups()
            if len(g) == 2:
                mo, d = int(g[0]), int(g[1])
                y = year
            else:
                y, mo, d = int(g[0]), int(g[1]), int(g[2])
            try:
                return datetime(y, mo, d).strftime("%Y-%m-%d")
            except ValueError:
                continue
        break
    return None


def _parse_last_user_time(
    history: List[HistoryMessage],
    reference_time: datetime,
) -> Optional[TemporalContext]:
    """
    职责：解析上轮用户句中的时间表达（如「今天」「昨天」）。
    追问场景下当 assistant 无日期时作为 fallback。
    返回 TemporalContext 或 None。
    """
    for i in range(len(history) - 1, -1, -1):
        msg = history[i]
        if getattr(msg, "role", None) == "user":
            content = (getattr(msg, "content", "") or "").strip()
            if content:
                ctx = TemporalResolver.resolve(content, reference_time=reference_time)
                if ctx.resolved and ctx.reference_date:
                    return ctx
            break
    return None


def build_follow_up_temporal_context(
    history: List[HistoryMessage],
    current_date_str: str,
) -> Optional[tuple[str, TemporalContext]]:
    """
    追问场景下的时间继承编排。
    优先级：assistant_event_time > last_user_time > current_date。
    当前句无时间时才调用，故 query_time 已为 None。
    返回 (time_source, TemporalContext)，time_source 为 "inherited" | "last_user" | "default"。
    """
    if not history or len(history) < 2:
        return None
    try:
        ref_dt = datetime.strptime(current_date_str, "%Y-%m-%d")
    except ValueError:
        ref_dt = datetime.now()
    current_date = ref_dt.strftime("%Y-%m-%d")

    event_date = _parse_assistant_event_time(history, ref_dt.year)
    if event_date:
        dt = datetime.strptime(event_date, "%Y-%m-%d")
        dt_from = event_date
        dt_to = (dt + timedelta(days=1)).strftime("%Y-%m-%d")
        ctx = TemporalContext(
            reference_date=event_date,
            date_range_from=dt_from,
            date_range_to=dt_to,
            resolved=True,
            event_time=event_date,
            inherited_event_time=event_date,
            time_source="inherited",
        )
        return ("inherited", ctx)

    last_user_ctx = _parse_last_user_time(history, ref_dt)
    if last_user_ctx and last_user_ctx.reference_date:
        ctx = TemporalContext(
            reference_date=last_user_ctx.reference_date,
            date_range_from=last_user_ctx.date_range_from,
            date_range_to=last_user_ctx.date_range_to,
            resolved=True,
            time_source="last_user",
        )
        return ("last_user", ctx)

    dt_from = (ref_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    dt_to = (ref_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    ctx = TemporalContext(
        reference_date=current_date,
        date_range_from=dt_from,
        date_range_to=dt_to,
        resolved=True,
        time_source="default",
    )
    return ("default", ctx)
