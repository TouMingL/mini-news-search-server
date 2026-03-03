# app/services/temporal_scope.py
"""
时间范围与回答范围推导层

职责：根据 TemporalContext + follow_up_type（+ time_intent, time_sensitivity）
统一计算「检索时间范围」与「回答范围日期（answer_scope_date）」。
下游仅消费这两类结果，不再混用 reference_date 或对 object_switch 做特判。

时间上下文不脱钩：仅消费已产出的 TemporalContext 与追问类型，不引入新时间来源。
"""
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from loguru import logger
from app.services.schemas import (
    TemporalContext,
    FollowUpType,
    TimeIntent,
    AnswerScopeMode,
)


def compute_answer_scope_date(
    temporal_context: Optional[TemporalContext],
    follow_up_type:   Optional[FollowUpType],
) -> Optional[str]:
    """
    计算本问的「回答范围日期」：回答是否必须限定在某一日。

    有值则：注入日期约束、has_evidence_for_date、时间对齐校验。
    None 则：无单日约束，不注入目标日期、不做时间对齐。

    业务规则（与计划表一致）：
    - time_switch + resolved -> reference_date
    - time_switch + not resolved -> None
    - event_continue -> reference_date（继承）
    - object_switch -> None（换对象，不限定单日）
    - None（新问）+ resolved -> reference_date
    - None（新问）+ not resolved -> None
    """
    if not temporal_context:
        return None
    ref = temporal_context.reference_date
    resolved = temporal_context.resolved

    if follow_up_type == "object_switch":
        return None
    if follow_up_type == "event_continue":
        return ref
    if follow_up_type == "time_switch":
        return ref if resolved else None
    # follow_up_type is None: 新问
    return ref if resolved else None


def compute_answer_scope_mode(
    context: List[Dict],
    answer_scope_date: Optional[str],
    current_date: Optional[str] = None,
) -> AnswerScopeMode:
    """
    检索完成后判定回答策略：目标日有事件则严格约束，仅有该日报道而事件在前几日则宽松。

    返回:
      "strict_date"   — 仅可播报事件日=目标日（或无目标日时的默认值）。
      "report_day_ok" — 无目标日事件但有目标日报道，可「未找到当天…；根据该日报道，前几日有…」。
    """
    if not answer_scope_date or not context:
        return "strict_date"

    canon = answer_scope_date.strip()[:10]
    try:
        dt = datetime.strptime(canon, "%Y-%m-%d")
        y, m, d = dt.year, dt.month, dt.day
    except ValueError:
        return "strict_date"

    event_date_variants = {canon, f"{y}年{m}月{d}日", f"{m}月{d}日", f"{m:02d}-{d:02d}"}

    has_event_on_date = False
    has_published_on_date = False

    for item in context:
        # -- 判断是否有事件日=目标日 --
        ets = item.get("event_time_timestamp")
        if ets is not None:
            try:
                item_date = datetime.fromtimestamp(float(ets)).strftime("%Y-%m-%d")
                if item_date == canon:
                    has_event_on_date = True
            except (ValueError, TypeError, OSError):
                pass

        ret = item.get("rule_event_time")
        if ret and any(v in str(ret) for v in event_date_variants):
            has_event_on_date = True

        content = (item.get("content") or "") + (item.get("title") or "")
        if any(v in content for v in event_date_variants):
            has_event_on_date = True

        src = (item.get("source") or "").strip()
        if src == "赛况数据引擎":
            for line in (item.get("content") or "").split("\n"):
                if any(v in line for v in event_date_variants):
                    has_event_on_date = True

        # -- 判断是否有报道日=目标日 --
        pt = item.get("published_time")
        if pt:
            try:
                pt_str = str(pt)
                if "T" in pt_str:
                    pub_dt = datetime.fromisoformat(pt_str.replace("Z", "+00:00"))
                else:
                    pub_dt = datetime.strptime(pt_str[:10], "%Y-%m-%d")
                if pub_dt.strftime("%Y-%m-%d") == canon:
                    has_published_on_date = True
            except (ValueError, TypeError):
                pass

    logger.debug("compute_answer_scope_mode: has_event={}, has_published={}", has_event_on_date, has_published_on_date)
    if has_event_on_date:
        return "strict_date"
    if has_published_on_date:
        return "report_day_ok"
    return "strict_date"


def compute_retrieval_scope(
    temporal_context: Optional[TemporalContext],
    follow_up_type:   Optional[FollowUpType],
    time_intent:      Optional[TimeIntent],
    time_sensitivity: str = "none",
) -> Dict[str, Any]:
    """
    计算本轮的「检索时间范围」，返回与 search_params 兼容的字段 dict。
    供 Router 合并进 search_params，或由 Pipeline 合并后传入执行层。

    返回字段：filter_date_from, filter_date_to, time_filter_strategy,
    filter_event_time_from, filter_event_time_to, reference_datetime, time_sensitivity。
    """
    params: Dict[str, Any] = {
        "time_sensitivity": time_sensitivity,
        "reference_datetime": temporal_context.reference_date if temporal_context else None,
    }
    resolved = temporal_context and temporal_context.resolved
    has_date_range = (
        resolved
        and temporal_context
        and temporal_context.date_range_from
        and temporal_context.date_range_to
    )
    use_object_switch_window = follow_up_type == "object_switch"

    # 有明确日期范围时一律使用（含 object_switch 继承的事件日），避免退化为 recent 导致 filter_date_from 错误）
    if resolved and time_intent and has_date_range and not use_object_switch_window:
        ref_type = time_intent.time_reference_type
        if ref_type == "publish_time":
            params["time_filter_strategy"]   = "publish_time_only"
            params["filter_date_from"]       = temporal_context.date_range_from
            params["filter_date_to"]         = temporal_context.date_range_to
        elif ref_type == "event_time":
            params["time_filter_strategy"]   = "event_time_with_fallback"
            params["filter_event_time_from"] = temporal_context.reference_date
            params["filter_event_time_to"]   = temporal_context.reference_date
            ref_dt = datetime.strptime(temporal_context.reference_date, "%Y-%m-%d")
            params["filter_date_from"]       = temporal_context.reference_date
            params["filter_date_to"]         = (ref_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        elif ref_type == "ambiguous":
            params["time_filter_strategy"]   = "publish_time_only"
            params["filter_date_from"]       = temporal_context.date_range_from
            params["filter_date_to"]         = temporal_context.date_range_to
        else:
            params["filter_date_from"]       = temporal_context.date_range_from
            params["filter_date_to"]         = temporal_context.date_range_to
    elif has_date_range:
        # object_switch 等追问继承到的事件日也使用该范围，不再退化为 recent
        params["filter_date_from"] = temporal_context.date_range_from
        params["filter_date_to"]   = temporal_context.date_range_to
    else:
        today = datetime.now()
        if time_sensitivity == "realtime":
            params["filter_date_from"] = today.strftime("%Y-%m-%d")
        elif time_sensitivity == "recent":
            params["filter_date_from"] = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        elif time_sensitivity == "historical":
            params["filter_date_from"] = (today - timedelta(days=30)).strftime("%Y-%m-%d")

    return params
