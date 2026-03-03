# app/services/pipeline_modules/scores_formatter.py
"""
赛况数据格式化与读取。

产品场景：用户问「今天NBA比分」「马刺赛况」「东部排名」时，从本地数据引擎获取
结构化数据并格式化为人类可读的文本，直接作为回复或注入到 RAG context 中。
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


# ---------------------------------------------------------------------------
# Standings formatter
# ---------------------------------------------------------------------------

_CONFERENCES = ("东部", "西部")


def _format_standings(
    data: Dict[str, Any],
    queried_teams: Optional[List[str]] = None,
) -> str:
    """将赛季排名数据格式化为分区排名表，并校验用户所问球队是否存在于数据中。"""
    source = data.get("source", "")
    standings = data.get("standings") or []

    all_team_names: set[str] = set()
    present_confs: set[str] = set()
    lines = [f"【{source}】赛季排名", ""]

    for conf in standings:
        conference = conf.get("conference", "")
        teams = conf.get("teams") or []
        if not teams:
            continue
        present_confs.add(conference)
        lines.append(f"--- {conference} ---")
        for t in teams:
            name = t.get("team", "")
            all_team_names.add(name)
            rank = t.get("rank", "")
            w = t.get("w", "")
            l = t.get("l", "")
            pct = t.get("pct", "")
            recent = t.get("recent", "")
            lines.append(f"  {rank}. {name}  {w}胜{l}负 ({pct})  {recent}")
        lines.append("")

    missing_confs = [c for c in _CONFERENCES if c not in present_confs]
    for c in missing_confs:
        lines.append(f"注意：{c}排名数据暂无")

    if queried_teams:
        not_found = [t for t in queried_teams if t not in all_team_names]
        for t in not_found:
            lines.insert(1, f"注意：排名数据中未找到「{t}」的记录，请勿使用其他球队数据代替")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scores / boxscore formatter (existing logic)
# ---------------------------------------------------------------------------

def _format_scores_reply(
    data: Dict[str, Any],
    include_detail: bool = True,
    player_detail: bool = False,
    player_filter: Optional[List[str]] = None,
    queried_teams: Optional[List[str]] = None,
) -> str:
    """
    将赛况数据引擎返回的 data 格式化为纯文本回复。
    内部按 data["kind"] 分发：standings 走排名格式化，其余走比分格式化。
    """
    kind = data.get("kind", "")
    if kind == "standings":
        return _format_standings(data, queried_teams=queried_teams)

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


# ---------------------------------------------------------------------------
# LLM context slimming（按数据实体过滤，缩小 prompt 范围）
# ---------------------------------------------------------------------------

_CONF_KEYWORDS: Dict[str, str] = {
    "东部": "东部", "东区": "东部",
    "西部": "西部", "西区": "西部",
}


def _detect_conference(query: str) -> Optional[str]:
    """从查询文本中识别目标赛区（东部/西部），无法识别则返回 None。"""
    for kw, conf in _CONF_KEYWORDS.items():
        if kw in query:
            return conf
    return None


def _detect_teams_in_query(query: str) -> List[str]:
    """从查询文本中用队名别名表正则匹配所有提及的球队，返回标准队名列表。"""
    from app.services.tools.score_tool import _TEAM_ALIASES
    matched: List[str] = []
    q_lower = query.lower()
    for canonical, aliases in _TEAM_ALIASES.items():
        for alias in aliases:
            if alias.isascii():
                if alias.lower() in q_lower:
                    matched.append(canonical)
                    break
            else:
                if alias in query:
                    matched.append(canonical)
                    break
    return matched


def _slim_standings_context(
    data: Dict[str, Any],
    query: str,
    queried_teams: Optional[List[str]] = None,
) -> str:
    """
    按可匹配的数据实体（赛区、队名）裁剪排名表，分析推理留给 LLM。

    过滤维度（按优先级叠加）：
    1. 赛区：query 提及「东部/西部」→ 只保留该赛区
    2. 球队：query 提及具体队名     → 只保留该队所在赛区（保持排名上下文完整）
    3. 均未命中                      → 保留全部数据
    """
    source    = data.get("source", "")
    standings = data.get("standings") or []

    target_conf  = _detect_conference(query)
    mentioned    = set(queried_teams or []) | set(_detect_teams_in_query(query))

    # 如果提及了具体球队但没指定赛区，找出球队所在赛区作为过滤条件
    if mentioned and not target_conf:
        for conf in standings:
            team_names = {t.get("team", "") for t in (conf.get("teams") or [])}
            if mentioned & team_names:
                target_conf = conf.get("conference")
                break

    lines = [f"【{source}】赛季排名"]
    for conf in standings:
        conference = conf.get("conference", "")
        teams      = conf.get("teams") or []
        if not teams:
            continue
        if target_conf and conference != target_conf:
            continue

        lines.append(f"--- {conference} ---")
        for t in teams:
            rank   = t.get("rank", "")
            name   = t.get("team", "")
            w      = t.get("w", "")
            l_val  = t.get("l", "")
            pct    = t.get("pct", "")
            recent = t.get("recent", "")
            lines.append(f"  {rank}. {name}  {w}胜{l_val}负 ({pct})  {recent}")

    return "\n".join(lines)


def _slim_scores_context(
    data: Dict[str, Any],
    query: str,
    intent: Optional[str] = None,
    was_filtered: bool = False,
    want_detail: bool = False,
    player_filter: Optional[List[str]] = None,
    queried_teams: Optional[List[str]] = None,
) -> str:
    """
    将赛况数据按查询意图裁剪为最小上下文字符串，供后续 LLM 生成精准答案使用。

    standings 走智能行数裁剪；比分/盒子分数复用现有格式化逻辑（已按球队过滤）。
    """
    kind = data.get("kind", "")
    if kind == "standings":
        return _slim_standings_context(data, query, queried_teams=queried_teams)
    # 比分汇总 / 盒子分数：复用现有格式化，已处理球队过滤与节次/球员展开
    return _format_scores_reply(
        data,
        include_detail=was_filtered or want_detail,
        player_detail=want_detail,
        player_filter=player_filter,
        queried_teams=queried_teams,
    )


# ---------------------------------------------------------------------------
# Read + route entry (called by pipeline)
# ---------------------------------------------------------------------------

def _read_nba_scores_for_query(
    date: str, query: str, intent: Optional[str] = None,
) -> tuple[Dict[str, Any], bool]:
    """
    NBA 数据统一读取入口：按 query + RouteLLM intent 路由到对应数据源，返回 (data, was_filtered)。
    was_filtered=True 表示只展示了部分场次（按队名筛过），可展示节次+球员。
    """
    from app.services.tools.score_tool import query_nba_data
    data = query_nba_data(date, query, intent=intent)
    return data, data.get("was_filtered", False)
