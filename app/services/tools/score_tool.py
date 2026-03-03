"""
NBA 赛况数据引擎：从指定 JSON 或 parsed_boxscore 目录读取比赛比分与信息，原样返回。

- 旧版：单文件 sohu_nba_block4 格式（source/block/matches）。
- 新版：parsed_boxscore/{date}/{Away}_vs_{Home}/ 下 score.json + 主客队 JSON，
  含节次、球员、合计、命中率等详细数据。

设计原则：仅做文件读取与结构化解析，不调用 LLM，不生成任何文本。
"""

import json
from pathlib import Path
from typing import List, Dict, Any, Optional

# 旧版单文件比分路径（无配置时回退用）
_DEFAULT_LEGACY_PATH = Path(
    r"C:\Users\HX\Documents\KP\news-search\data\sohu_nba_block4_scores_2026-02-26.json"
)
NBA_SCORES_JSON_PATH = _DEFAULT_LEGACY_PATH  # 兼容旧引用；实际优先用 config + boxscore

# 队名别名：canonical 中文名 -> 匹配 query 时的别名（含英文、简称、城市名），实现「今日热火打得怎么样」「Heat vs 76人」等效，无需 LLM
_TEAM_ALIASES: Dict[str, List[str]] = {
    "热火": ["热火", "Heat", "迈阿密热火", "迈阿密"],
    "76人": ["76人", "76ers", "费城76人", "费城"],
    "湖人": ["湖人", "Lakers", "洛杉矶湖人", "洛杉矶"],
    "太阳": ["太阳", "Suns", "菲尼克斯太阳", "菲尼克斯"],
    "凯尔特人": ["凯尔特人", "Celtics", "波士顿凯尔特人", "波士顿"],
    "勇士": ["勇士", "Warriors", "金州勇士", "金州"],
    "雄鹿": ["雄鹿", "Bucks", "密尔沃基雄鹿", "密尔沃基"],
    "掘金": ["掘金", "Nuggets", "丹佛掘金", "丹佛"],
    "快船": ["快船", "Clippers", "洛杉矶快船"],
    "尼克斯": ["尼克斯", "Knicks", "纽约尼克斯", "纽约"],
    "公牛": ["公牛", "Bulls", "芝加哥公牛", "芝加哥"],
    "开拓者": ["开拓者", "Trail Blazers", "Blazers", "波特兰开拓者", "波特兰"],
    "爵士": ["爵士", "Jazz", "犹他爵士", "犹他"],
    "鹈鹕": ["鹈鹕", "Pelicans", "新奥尔良鹈鹕", "新奥尔良"],
    "独行侠": ["独行侠", "Mavericks", "小牛", "达拉斯独行侠", "达拉斯"],
    "国王": ["国王", "Kings", "萨克拉门托国王", "萨克拉门托"],
    "老鹰": ["老鹰", "Hawks", "亚特兰大老鹰", "亚特兰大"],
    "奇才": ["奇才", "Wizards", "华盛顿奇才", "华盛顿"],
    "魔术": ["魔术", "Magic", "奥兰多魔术", "奥兰多"],
    "火箭": ["火箭", "Rockets", "休斯顿火箭", "休斯顿"],
    "篮网": ["篮网", "Nets", "布鲁克林篮网", "布鲁克林"],
    "马刺": ["马刺", "Spurs", "圣安东尼奥马刺", "圣安东尼奥"],
    "步行者": ["步行者", "Pacers", "印第安纳步行者", "印第安纳"],
    "黄蜂": ["黄蜂", "Hornets", "夏洛特黄蜂", "夏洛特"],
    "活塞": ["活塞", "Pistons", "底特律活塞", "底特律"],
    "骑士": ["骑士", "Cavaliers", "Cavs", "克利夫兰骑士", "克利夫兰"],
    "森林狼": ["森林狼", "Timberwolves", "Wolves", "明尼苏达森林狼", "明尼苏达"],
}


def _team_aliases(team_name: str) -> List[str]:
    """返回某队名的所有匹配用别名（含自身），用于 query 匹配。"""
    if not team_name or not team_name.strip():
        return []
    t = team_name.strip()
    return list(_TEAM_ALIASES.get(t, [t]))


def _validate_match(m: Any) -> Dict[str, str]:
    """校验单条比赛为 dict 且包含必要字段（旧版格式）。"""
    if not isinstance(m, dict):
        raise ValueError(f"单条比赛必须为 dict，实际类型: {type(m)}")
    out: Dict[str, str] = {}
    for key in ("date", "place", "home_team", "home_score", "away_team", "away_score", "status", "match_time", "link"):
        val = m.get(key)
        out[key] = str(val) if val is not None else ""
    return out


def _get_config_paths() -> tuple[Optional[Path], Optional[Path]]:
    """从 Flask 配置或环境变量获取比分文件路径与 boxscore 根路径。"""
    try:
        from flask import current_app
        json_path = current_app.config.get("NBA_SCORES_JSON_PATH")
        root = current_app.config.get("NBA_BOXSCORE_ROOT")
    except Exception:
        import os
        json_path = os.getenv("NBA_SCORES_JSON_PATH", "")
        root = os.getenv("NBA_BOXSCORE_ROOT", r"C:\Users\HX\Documents\KP\news-search\data\parsed_boxscore")
    p1 = Path(json_path).resolve() if json_path else None
    p2 = Path(root).resolve() if root else None
    return p1, p2


def list_nba_games_for_date(date_str: str) -> List[Dict[str, str]]:
    """
    列出指定日期的 NBA 比赛（仅读 score.json 与主客队 JSON 获取队名，不读球员详情）。
    用于先列出再按 query 筛选，再只读匹配比赛的详细数据。

    Returns:
        [{"rel_path": "76ers_vs_Heat", "home_team": "76人", "away_team": "热火", "game_date": "2026-02-27"}, ...]
    """
    _, boxroot = _get_config_paths()
    if not boxroot or not boxroot.is_dir():
        return []
    date_dir = boxroot / date_str
    if not date_dir.is_dir():
        return []
    result: List[Dict[str, str]] = []
    for game_dir in sorted(date_dir.iterdir()):
        if not game_dir.is_dir():
            continue
        score_path = game_dir / "score.json"
        if not score_path.is_file():
            continue
        try:
            score_data = json.loads(score_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(score_data, dict):
            continue
        rounds_raw = score_data.get("round") or []
        if not isinstance(rounds_raw, list) or not rounds_raw:
            continue
        total_row = rounds_raw[-1]
        if not isinstance(total_row, dict):
            continue
        team_keys = [k for k in total_row if k != "节次" and total_row.get(k) is not None]
        if len(team_keys) != 2:
            continue

        home_team: Optional[str] = None
        away_team: Optional[str] = None
        for f in game_dir.iterdir():
            if f.suffix != ".json" or f.name == "score.json":
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            team = data.get("team")
            if not isinstance(team, str):
                continue
            if data.get("is_home") is True:
                home_team = team
            else:
                away_team = team
        if not home_team:
            home_team = team_keys[0]
        if not away_team:
            away_team = team_keys[1]
        result.append({
            "rel_path": game_dir.name,
            "home_team": home_team,
            "away_team": away_team,
            "game_date": str(score_data.get("game_date", date_str)),
        })
    return result


def filter_games_by_query(
    games: List[Dict[str, str]],
    query: str,
) -> Optional[List[str]]:
    """
    根据用户 query 筛选出匹配的比赛 rel_path 列表。
    使用队名别名表：query 中出现某场主队或客队的任一别名（中文/英文/城市名等）即保留该场；
    若没有任何一场被匹配则返回 None（展示全部）。
    实现「今日热火打得怎么样」「热火vs76人」「Heat vs 76ers」等效。
    """
    if not query or not games:
        return None
    q = query.strip()
    if not q:
        return None
    q_lower = q.lower()
    matched: List[str] = []
    for g in games:
        home = (g.get("home_team") or "").strip()
        away = (g.get("away_team") or "").strip()
        home_aliases = _team_aliases(home)
        away_aliases = _team_aliases(away)
        def _query_contains_alias(aliases: List[str]) -> bool:
            for a in aliases:
                if not a:
                    continue
                if a.isascii():
                    if a.lower() in q_lower:
                        return True
                else:
                    if a in q:
                        return True
            return False
        if _query_contains_alias(home_aliases):
            matched.append(g["rel_path"])
            continue
        if _query_contains_alias(away_aliases):
            matched.append(g["rel_path"])
    if not matched:
        return None
    return matched


def _read_boxscore_date(
    boxroot: Path,
    date_str: str,
    game_rel_paths: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    从 parsed_boxscore/{date}/ 读取比赛，返回与 read_nba_scores 兼容的结构，
    并附带 matches_detail（节次、球员、合计等）。
    game_rel_paths: 仅读取这些子目录名（如 ["76ers_vs_Heat"]）；None 表示读取该日全部比赛。
    目录不存在或为空时返回 None。
    """
    date_dir = boxroot / date_str
    if not date_dir.is_dir():
        return None
    if game_rel_paths is not None:
        game_dirs = [date_dir / p for p in game_rel_paths if (date_dir / p).is_dir()]
        game_dirs.sort(key=lambda d: d.name)
    else:
        game_dirs = [d for d in date_dir.iterdir() if d.is_dir()]
        game_dirs.sort(key=lambda d: d.name)
    if not game_dirs:
        return None

    matches: List[Dict[str, str]] = []
    matches_detail: List[Dict[str, Any]] = []

    for game_dir in game_dirs:
        score_path = game_dir / "score.json"
        if not score_path.is_file():
            continue
        try:
            score_data = json.loads(score_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(score_data, dict):
            continue
        game_id = score_data.get("game_id", "")
        game_date = score_data.get("game_date", date_str)
        rounds_raw = score_data.get("round") or []
        if not isinstance(rounds_raw, list) or not rounds_raw:
            continue

        # 最后一行为总分：{"节次":"总分","湖人":110,"太阳":113}
        total_row = rounds_raw[-1] if rounds_raw else {}
        if not isinstance(total_row, dict):
            continue
        team_keys = [k for k in total_row if k != "节次" and total_row.get(k) is not None]
        if len(team_keys) != 2:
            continue
        score_a = total_row.get(team_keys[0])
        score_b = total_row.get(team_keys[1])

        # 确定主客：查找 (home).json / (away).json
        home_team: Optional[str] = None
        away_team: Optional[str] = None
        home_score_val: Optional[str] = None
        away_score_val: Optional[str] = None
        home_json_path: Optional[Path] = None
        away_json_path: Optional[Path] = None

        for f in game_dir.iterdir():
            if not f.suffix == ".json" or f.name == "score.json":
                continue
            name = f.stem
            if "(home)" in name:
                home_json_path = f
            elif "(away)" in name:
                away_json_path = f

        home_data: Optional[Dict] = None
        away_data: Optional[Dict] = None
        if home_json_path and home_json_path.is_file():
            try:
                home_data = json.loads(home_json_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        if away_json_path and away_json_path.is_file():
            try:
                away_data = json.loads(away_json_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        if home_data and isinstance(home_data.get("team"), str):
            home_team = home_data["team"]
        if away_data and isinstance(away_data.get("team"), str):
            away_team = away_data["team"]

        if not home_team or not away_team:
            home_team = home_team or team_keys[0]
            away_team = away_team or team_keys[1]

        if home_team in total_row and away_team in total_row:
            home_score_val = str(total_row[home_team])
            away_score_val = str(total_row[away_team])
        else:
            home_score_val = str(score_a) if home_team == team_keys[0] else str(score_b)
            away_score_val = str(score_b) if home_team == team_keys[0] else str(score_a)

        game_status = score_data.get("game_status") or score_data.get("status")
        status_str = (game_status.strip() if isinstance(game_status, str) and game_status.strip() else "已结束")

        matches.append({
            "date": str(game_date),
            "place": "",
            "match_time": "",
            "home_team": home_team,
            "home_score": home_score_val or "",
            "away_team": away_team,
            "away_score": away_score_val or "",
            "status": status_str,
            "link": "",
        })

        # 节次
        round_list: List[Dict[str, Any]] = []
        for r in rounds_raw:
            if isinstance(r, dict):
                round_list.append(dict(r))

        # 球员与合计（仅保留上场有数据的，避免冗长）
        def _summarize_players(players: List[Dict], max_show: int = 10) -> List[Dict[str, Any]]:
            if not isinstance(players, list):
                return []
            out = []
            for p in players:
                if not isinstance(p, dict):
                    continue
                pts = p.get("得分")
                if pts is None or (isinstance(pts, str) and pts.strip() == ""):
                    continue
                out.append({
                    "姓名": p.get("姓名", ""),
                    "位置": p.get("位置", ""),
                    "时间": p.get("时间", ""),
                    "得分": p.get("得分", ""),
                    "篮板": p.get("篮板", ""),
                    "助攻": p.get("助攻", ""),
                    "投篮": p.get("投篮", ""),
                    "3分": p.get("3分", ""),
                    "罚球": p.get("罚球", ""),
                    "抢断": p.get("抢断", ""),
                    "封盖": p.get("封盖", ""),
                    "失误": p.get("失误", ""),
                    "+/-": p.get("+/-", ""),
                })
            return out[:max_show]

        home_players = _summarize_players(home_data.get("球员", [])) if home_data else []
        away_players = _summarize_players(away_data.get("球员", [])) if away_data else []
        home_totals = home_data.get("合计") if isinstance(home_data, dict) else None
        away_totals = away_data.get("合计") if isinstance(away_data, dict) else None
        if not isinstance(home_totals, dict):
            home_totals = None
        if not isinstance(away_totals, dict):
            away_totals = None

        matches_detail.append({
            "game_id": game_id,
            "round": round_list,
            "home_team": home_team,
            "away_team": away_team,
            "home_players": home_players,
            "away_players": away_players,
            "home_totals": home_totals,
            "away_totals": away_totals,
        })

    if not matches:
        return None
    return {
        "source": "parsed_boxscore",
        "block": date_str,
        "matches": matches,
        "matches_detail": matches_detail,
    }


def read_nba_scores(
    file_path: Optional[Path] = None,
    date: Optional[str] = None,
    game_rel_paths: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    读取 NBA 比分数据。优先按日期从 parsed_boxscore 目录读取详细数据；否则从单文件读取旧版格式。

    Args:
        file_path: 旧版单文件 JSON 路径，显式传入时优先使用（忽略 date）。
        date: 日期 YYYY-MM-DD。当未传 file_path 且存在 boxscore 根配置时，从 parsed_boxscore/{date}/ 读取。
        game_rel_paths: 仅读取该日期下这些子目录（如 ["76ers_vs_Heat"]）；None 表示读取该日全部比赛。

    Returns:
        - source: str
        - block: str（日期或区块标识）
        - matches: list[dict]，每项含 date/home_team/home_score/away_team/away_score/status 等
        - matches_detail: list[dict]（仅 boxscore 时有），每项含 round、home_players、away_players、home_totals、away_totals
    """
    legacy_path, boxroot = _get_config_paths()
    use_legacy = file_path is not None or (legacy_path and legacy_path.is_file())

    if file_path is not None:
        path = Path(file_path)
    else:
        path = legacy_path or _DEFAULT_LEGACY_PATH

    if not use_legacy and date and boxroot and boxroot.is_dir():
        data = _read_boxscore_date(boxroot, date, game_rel_paths=game_rel_paths)
        if data is not None:
            return data

    if not path.is_file():
        raise FileNotFoundError(f"比分文件不存在: {path}")

    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("根节点必须为 JSON 对象")
    source = data.get("source")
    block = data.get("block")
    raw_matches = data.get("matches")
    if source is None:
        raise ValueError("缺少字段: source")
    if block is None:
        raise ValueError("缺少字段: block")
    if raw_matches is None:
        raise ValueError("缺少字段: matches")
    if not isinstance(raw_matches, list):
        raise ValueError("matches 必须为数组")

    matches: List[Dict[str, str]] = []
    for i, m in enumerate(raw_matches):
        try:
            matches.append(_validate_match(m))
        except ValueError as e:
            raise ValueError(f"matches[{i}] 校验失败: {e}") from e

    return {
        "source": str(source),
        "block": str(block),
        "matches": matches,
    }
