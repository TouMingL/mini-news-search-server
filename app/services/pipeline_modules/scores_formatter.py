# app/services/pipeline_modules/scores_formatter.py
"""
赛况数据格式化与读取。

产品场景：用户问「今天NBA比分」「马刺赛况」时，从本地赛况 JSON 读取数据
并格式化为人类可读的比分播报文本，直接作为回复或注入到 RAG context 中。
"""
from typing import Dict, List, Optional, Any


_SHOT_FIELDS = {"投篮", "3分", "罚球"}


def _format_shot_stat(key: str, val: str) -> str:
    """将 '9-19'（命中-出手）格式转为 '19投9中' 无歧义中文格式。"""
    if "-" in val:
        made, att = val.split("-", 1)
        return f"{key}{att}投{made}中"
    return f"{key}{val}"


def _format_player_stat_line(p: Dict[str, Any]) -> str:
    """将单个球员 dict 格式化为完整统计行，供 LLM 直接提取使用。"""
    parts = [p.get("姓名", "")]
    if p.get("时间"):
        parts.append(f"{p['时间']}分钟")
    for key in ("得分", "投篮", "3分", "罚球", "篮板", "助攻", "抢断", "封盖", "失误", "+/-"):
        val = p.get(key, "")
        if val != "":
            if key in _SHOT_FIELDS:
                parts.append(_format_shot_stat(key, val))
            else:
                parts.append(f"{key}{val}")
    return " | ".join(parts)


def _format_scores_reply(
    data: Dict[str, Any],
    include_detail: bool = True,
    player_detail: bool = False,
    player_filter: Optional[List[str]] = None,
) -> str:
    """
    将赛况数据引擎返回的 data 格式化为纯文本回复。
    player_filter 非空时只输出匹配球员的完整统计，其余球员仅出摘要。
    """
    source = data.get("source", "")
    block = data.get("block", "")
    matches = data.get("matches") or []
    details = (data.get("matches_detail") or []) if include_detail else []
    lines = [f"【{source}】{block}", ""]
    for i, m in enumerate(matches):
        date = m.get("date", "")
        place = m.get("place", "")
        match_time = m.get("match_time", "")
        home_team = m.get("home_team", "")
        home_score = m.get("home_score", "")
        away_team = m.get("away_team", "")
        away_score = m.get("away_score", "")
        status = m.get("status", "")
        line = f"{date} {place} {match_time} | {home_team}{home_score} - {away_team}{away_score}（{status}）"
        lines.append(line)
        if include_detail and i < len(details):
            d = details[i]
            round_list = d.get("round") or []
            for r in round_list:
                if not isinstance(r, dict):
                    continue
                jc = r.get("节次", "")
                keys = [k for k in r if k != "节次"]
                if jc and len(keys) >= 2:
                    parts = [f"{k}{r[k]}" for k in keys]
                    lines.append(f"  节次 {jc}: {' '.join(parts)}")
            home_players = d.get("home_players") or []
            away_players = d.get("away_players") or []
            if player_detail:
                for team_label, players in [(home_team, home_players), (away_team, away_players)]:
                    if not players:
                        continue
                    lines.append(f"  [{team_label}球员]")
                    for p in players:
                        name = p.get("姓名", "")
                        if player_filter and not any(kw in name for kw in player_filter):
                            continue
                        lines.append(f"  {_format_player_stat_line(p)}")
            elif home_players or away_players:
                hp_str = "；".join(f"{p.get('姓名','')}{p.get('得分','')}分" for p in home_players[:5])
                ap_str = "；".join(f"{p.get('姓名','')}{p.get('得分','')}分" for p in away_players[:5])
                if hp_str:
                    lines.append(f"  {home_team} 主要得分: {hp_str}")
                if ap_str:
                    lines.append(f"  {away_team} 主要得分: {ap_str}")
    return "\n".join(lines)


def _read_nba_scores_for_query(date: str, query: str) -> tuple[Dict[str, Any], bool]:
    """
    按日期列出比赛、用 query 筛选出匹配场次后，只读取这些比赛的详细数据；无 boxscore 时回退到旧版单文件。
    Returns:
        (data, was_filtered): was_filtered=True 表示只展示了部分场次（按队名筛过），可展示节次+球员；False 表示全部显示，仅展示总分。
    """
    from app.services.tools.score_tool import (
        list_nba_games_for_date,
        filter_games_by_query,
        read_nba_scores,
    )
    games = list_nba_games_for_date(date)
    if games:
        paths = filter_games_by_query(games, query or "")
        was_filtered = paths is not None
        data = read_nba_scores(date=date, game_rel_paths=paths)
        return data, was_filtered
    return read_nba_scores(date=date), False
