# app/services/temporal_scope.py
"""
时间范围与回答范围推导层

职责：根据 TemporalContext + follow_up_type（+ time_intent, time_sensitivity）
统一计算「检索时间范围」与「回答范围日期（answer_scope_date）」。
下游仅消费这两类结果，不再混用 reference_date 或对 object_switch 做特判。

时间上下文不脱钩：仅消费已产出的 TemporalContext 与追问类型，不引入新时间来源。
"""
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from app.services.schemas import (
    TemporalContext,
    FollowUpType,
    TimeIntent,
)


def compute_answer_scope_date(
    temporal_context: Optional[TemporalContext],
    follow_up_type: Optional[FollowUpType],
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


def compute_retrieval_scope(
    temporal_context: Optional[TemporalContext],
    follow_up_type: Optional[FollowUpType],
    time_intent: Optional[TimeIntent],
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

    if resolved and time_intent and has_date_range and not use_object_switch_window:
        ref_type = time_intent.time_reference_type
        if ref_type == "publish_time":
            params["time_filter_strategy"] = "publish_time_only"
            params["filter_date_from"] = temporal_context.date_range_from
            params["filter_date_to"] = temporal_context.date_range_to
        elif ref_type == "event_time":
            params["time_filter_strategy"] = "event_time_with_fallback"
            params["filter_event_time_from"] = temporal_context.reference_date
            params["filter_event_time_to"] = temporal_context.reference_date
            ref_dt = datetime.strptime(temporal_context.reference_date, "%Y-%m-%d")
            params["filter_date_from"] = temporal_context.reference_date
            params["filter_date_to"] = (ref_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        elif ref_type == "ambiguous":
            params["time_filter_strategy"] = "publish_time_only"
            params["filter_date_from"] = temporal_context.date_range_from
            params["filter_date_to"] = temporal_context.date_range_to
        else:
            params["filter_date_from"] = temporal_context.date_range_from
            params["filter_date_to"] = temporal_context.date_range_to
    elif has_date_range and not use_object_switch_window:
        params["filter_date_from"] = temporal_context.date_range_from
        params["filter_date_to"] = temporal_context.date_range_to
    else:
        today = datetime.now()
        if time_sensitivity == "realtime":
            params["filter_date_from"] = today.strftime("%Y-%m-%d")
        elif time_sensitivity == "recent":
            params["filter_date_from"] = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        elif time_sensitivity == "historical":
            params["filter_date_from"] = (today - timedelta(days=30)).strftime("%Y-%m-%d")

    return params
