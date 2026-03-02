# app/services/temporal_resolver.py
"""
时间解析层 - TemporalResolver

职责：将用户查询中的相对时间表达（昨天/前天/今天/上周等）解析为绝对日期。
输出供检索过滤、查询扩展、生成层使用的 TemporalContext。
纯规则实现，不调用 LLM。
"""
import re
from datetime import datetime, timedelta
from typing import Optional

from app.services.schemas import TemporalContext


# 相对时间表达 -> 天数偏移（0=今天，-1=昨天，-2=前天）
_SINGLE_DAY_PATTERNS = [
    (r"今天|今日", 0),
    (r"昨天|昨日", -1),
    (r"前天|前日", -2),
]


def _resolve_single_day(query: str, ref: datetime) -> Optional[str]:
    """解析单日表达（昨天/前天/今天），返回 YYYY-MM-DD。"""
    lower = query.strip().lower()
    for pattern, days_offset in _SINGLE_DAY_PATTERNS:
        if re.search(pattern, lower):
            target = ref + timedelta(days=days_offset)
            return target.strftime("%Y-%m-%d")
    return None


def _get_date_range_for_single_day(reference_date: str, buffer_days: int = 1) -> tuple[str, str]:
    """
    为单日查询生成检索用日期窗口。
    报道可能在前一日或后一日发布，故前后各加 buffer_days 天。
    """
    dt = datetime.strptime(reference_date, "%Y-%m-%d")
    dt_from = dt - timedelta(days=buffer_days)
    dt_to = dt + timedelta(days=buffer_days)
    return dt_from.strftime("%Y-%m-%d"), dt_to.strftime("%Y-%m-%d")


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
            reference_time: 参考时间（默认为 now），用于计算「昨天」「前天」等
            
        Returns:
            TemporalContext: 解析结果
        """
        if reference_time is None:
            reference_time = datetime.now()
        
        reference_date = _resolve_single_day(query, reference_time)
        
        if reference_date is None:
            return TemporalContext(
                reference_date=None,
                date_range_from=None,
                date_range_to=None,
                resolved=False,
            )
        
        date_range_from, date_range_to = _get_date_range_for_single_day(reference_date)
        
        return TemporalContext(
            reference_date=reference_date,
            date_range_from=date_range_from,
            date_range_to=date_range_to,
            resolved=True,
        )


def get_temporal_resolver() -> TemporalResolver:
    """获取 TemporalResolver 实例（无状态，直接 new）"""
    return TemporalResolver()
