# app/services/temporal_resolver.py
"""
时间解析层 - TemporalResolver

职责：将用户查询中的相对时间表达（昨天/前天/今天/上周六/上周/上个月等）解析为绝对日期。
输出供检索过滤、查询扩展、生成层使用的 TemporalContext。
纯规则实现，不调用 LLM。正确处理 2 月、闰年等由 datetime/timedelta 自动处理。
"""
import re
from datetime import datetime, timedelta
from typing import Optional

from app.services.schemas import TemporalContext


# 单日偏移：今天/ yesterday/前天
_SINGLE_DAY_OFFSETS = [
    (r"今天|今日", 0),
    (r"昨天|昨日", -1),
    (r"前天|前日", -2),
]

# 中文星期 -> Python weekday（周一=0, 周日=6）
_WEEKDAY_MAP = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
}


def _resolve_single_day(query: str, ref: datetime) -> Optional[str]:
    """解析单日表达（昨天/前天/今天），返回 YYYY-MM-DD。"""
    lower = query.strip().lower()
    for pattern, days_offset in _SINGLE_DAY_OFFSETS:
        if re.search(pattern, lower):
            target = ref + timedelta(days=days_offset)
            return target.strftime("%Y-%m-%d")
    return None


def _resolve_last_weekday(query: str, ref: datetime) -> Optional[str]:
    """
    解析「上周X」（上周一~周日），返回 YYYY-MM-DD。
    上周六：ref 为周一则 -2 天；ref 为周六则 -7 天。datetime 自动处理 2 月、闰年。
    """
    m = re.search(r"上周([一二三四五六日天])", query)
    if not m:
        return None
    wd = _WEEKDAY_MAP.get(m.group(1))
    if wd is None:
        return None
    days_back = (ref.weekday() - wd + 7) % 7
    if days_back == 0:
        days_back = 7
    target = ref - timedelta(days=days_back)
    return target.strftime("%Y-%m-%d")


def _resolve_last_week_range(ref: datetime) -> tuple[str, str]:
    """解析「上周」为日期范围：上周一 00:00 ~ 上周日 23:59。"""
    days_to_sunday = (ref.weekday() + 1) % 7
    if days_to_sunday == 0:
        days_to_sunday = 7
    last_sunday = ref - timedelta(days=days_to_sunday)
    last_monday = last_sunday - timedelta(days=6)
    return last_monday.strftime("%Y-%m-%d"), last_sunday.strftime("%Y-%m-%d")


def _resolve_last_month_range(ref: datetime) -> tuple[str, str]:
    """解析「上个月/上月」为日期范围：上月 1 日 ~ 上月最后一天。"""
    first_this_month = ref.replace(day=1)
    last_last_month = first_this_month - timedelta(days=1)
    first_last_month = last_last_month.replace(day=1)
    return first_last_month.strftime("%Y-%m-%d"), last_last_month.strftime("%Y-%m-%d")


def _get_date_range_for_single_day(reference_date: str, buffer_days: int = 1) -> tuple[str, str]:
    """
    为单日查询生成检索用日期窗口。
    报道可能在前一日或后一日发布，故前后各加 buffer_days 天。
    """
    dt = datetime.strptime(reference_date, "%Y-%m-%d")
    dt_from = dt - timedelta(days=buffer_days)
    dt_to = dt + timedelta(days=buffer_days)
    return dt_from.strftime("%Y-%m-%d"), dt_to.strftime("%Y-%m-%d")


_EXPLICIT_DATE_RE = re.compile(
    r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]"
)


def _resolve_explicit_date(query: str, ref: datetime) -> Optional[str]:
    """解析显式日期（X月X日、YYYY年X月X日），年份缺省时取 ref 同年。返回 YYYY-MM-DD。"""
    m = _EXPLICIT_DATE_RE.search(query)
    if not m:
        return None
    year = int(m.group(1)) if m.group(1) else ref.year
    month, day = int(m.group(2)), int(m.group(3))
    try:
        target = datetime(year, month, day)
        return target.strftime("%Y-%m-%d")
    except ValueError:
        return None


def _resolve_relative_time(query: str, ref: datetime) -> Optional[tuple[str, str, str]]:
    """
    统一解析时间表达，返回 (reference_date, date_range_from, date_range_to) 或 None。
    优先级：单日相对 > 上周X > 上周 > 上月 > 显式日期。
    """
    q = query.strip()

    # 1. 单日：今天/昨天/前天
    ref_date = _resolve_single_day(q, ref)
    if ref_date:
        dt_from, dt_to = _get_date_range_for_single_day(ref_date)
        return (ref_date, dt_from, dt_to)

    # 2. 上周X（上周一~周日）
    ref_date = _resolve_last_weekday(q, ref)
    if ref_date:
        dt_from, dt_to = _get_date_range_for_single_day(ref_date)
        return (ref_date, dt_from, dt_to)

    # 3. 上周（整周范围）
    if re.search(r"上周(?![一二三四五六日天])", q) or re.search(r"^上周$", q):
        dt_from, dt_to = _resolve_last_week_range(ref)
        ref_date = dt_from
        return (ref_date, dt_from, dt_to)

    # 4. 上个月 / 上月
    if re.search(r"上(个)?月", q):
        dt_from, dt_to = _resolve_last_month_range(ref)
        ref_date = dt_from
        return (ref_date, dt_from, dt_to)

    # 5. 显式日期（3月2日、2026年3月2日）
    ref_date = _resolve_explicit_date(q, ref)
    if ref_date:
        dt_from, dt_to = _get_date_range_for_single_day(ref_date)
        return (ref_date, dt_from, dt_to)

    return None


class TemporalResolver:
    """
    时间解析器

    输入：用户 query、参考时间（默认 now）
    输出：TemporalContext（reference_date、date_range_from/to、resolved）
    """

    @staticmethod
    def resolve(
        query: str,
        reference_time: Optional[datetime] = None,
    ) -> TemporalContext:
        """
        解析用户查询中的时间表达。

        Args:
            query: 用户原始查询
            reference_time: 参考时间（默认为 now），用于计算昨天、上周六等

        Returns:
            TemporalContext: 解析结果
        """
        if reference_time is None:
            reference_time = datetime.now()

        result = _resolve_relative_time(query, reference_time)

        if result is None:
            return TemporalContext(
                reference_date=None,
                date_range_from=None,
                date_range_to=None,
                resolved=False,
            )

        reference_date, date_range_from, date_range_to = result
        return TemporalContext(
            reference_date=reference_date,
            date_range_from=date_range_from,
            date_range_to=date_range_to,
            resolved=True,
        )


def get_temporal_resolver() -> TemporalResolver:
    """获取 TemporalResolver 实例（无状态，直接 new）"""
    return TemporalResolver()
