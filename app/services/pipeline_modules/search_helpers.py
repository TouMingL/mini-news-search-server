# app/services/pipeline_modules/search_helpers.py
"""
检索辅助函数：语义分过滤、字面重叠、日期注入、按日期筛选等。

产品场景：在 RAG 检索后对结果做二次排序与过滤，确保返回给 LLM 的 context
在语义相关性和时间相关性上均达标。
"""
import re
from datetime import datetime
from typing import List, Dict, Optional, Any


def _filter_by_semantic_score(results: List[Dict], min_score: float) -> List[Dict]:
    """过滤掉 original_score 低于阈值的条目；无 original_score 时用 score 近似。"""
    out = []
    for item in results:
        s = item.get("original_score")
        if s is None:
            s = item.get("score", 0)
        if s >= min_score:
            out.append(item)
    return out


def _get_retrieval_min_semantic_score() -> float:
    try:
        from flask import current_app
        return float(current_app.config.get("RETRIEVAL_MIN_SEMANTIC_SCORE", 0.6))
    except Exception:
        return 0.6


def _term_overlap_ratio(query: str, doc_title: str, doc_content: str) -> float:
    """
    计算查询与文档（标题+正文）的字符级重叠比例，用于通用词面加分，不依赖任何实体表。
    重叠率 = |query 字符集 ∩ doc 字符集| / |query 字符集|，仅考虑非空白字符。
    """
    q = (query or "").strip()
    if not q:
        return 0.0
    doc = ((doc_title or "") + (doc_content or "")).strip()
    if not doc:
        return 0.0
    q_chars = {c for c in q if not c.isspace()}
    doc_chars = {c for c in doc if not c.isspace()}
    if not q_chars:
        return 0.0
    overlap = len(q_chars & doc_chars) / len(q_chars)
    return round(overlap, 6)


def _get_retrieval_term_overlap_boost_weight() -> float:
    try:
        from flask import current_app
        return float(current_app.config.get("RETRIEVAL_TERM_OVERLAP_BOOST_WEIGHT", 0.12))
    except Exception:
        return 0.12


def _filter_published_on_date(results: List[Dict[str, Any]], date_str: str) -> List[Dict[str, Any]]:
    """从检索结果中筛选 published_time 落在指定日期的条目，用于目标日事件缺失时降级为报道模式。"""
    canon = date_str.strip()[:10]
    out = []
    for item in results:
        pt = item.get("published_time")
        if not pt:
            continue
        try:
            pub_date = str(pt)[:10]
            if pub_date == canon:
                out.append(item)
        except Exception:
            continue
    return out


def _inject_date_into_query_for_search(query: str, reference_date: Optional[str]) -> str:
    """
    将查询中的相对时间表述替换为具体日期，供检索使用（新闻标题/正文多为具体日期，不含「上周六」等）。
    reference_date 为 YYYY-MM-DD；无则返回原 query。
    """
    if not reference_date or not query or len(reference_date) < 10:
        return query
    try:
        dt = datetime.strptime(reference_date[:10], "%Y-%m-%d")
        date_str = f"{dt.year}年{dt.month}月{dt.day}日"
    except ValueError:
        return query
    q = query
    replacements = [
        (r"上周[一二三四五六日天]", date_str),
        (r"上周", date_str),
        (r"上(个)?月", date_str),
        (r"昨天|昨日", date_str),
        (r"前天|前日", date_str),
        (r"今天|今日", date_str),
    ]
    for pattern, repl in replacements:
        q = re.sub(pattern, repl, q)
    return q
