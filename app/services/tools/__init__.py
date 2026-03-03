# Agent 工具包：仅做数据读取与返回，不经过 LLM，从根源上杜绝幻觉

from app.services.tools.score_tool import query_nba_data

__all__ = [
    "query_nba_data",
]
