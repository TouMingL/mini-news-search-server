"""
NBA 赛况数据引擎：按数据粒度分三个 Provider，由 NBAQueryRouter 按 query 关键词路由。

数据源（均按日期子目录组织）：
- parsed_boxscore/{date}/{Away}_vs_{Home}/  -- 单场详细数据（节次、球员技术统计）
- sohu_nba_block4_scores/{date}/scores.json -- 当天全部比赛的比分汇总
- sohu_nba_standings/{date}/standings.json  -- 赛季东/西部球队排名与战绩

设计原则：仅做文件读取与结构化解析，不调用 LLM，不生成任何文本。
"""

import json
from pathlib import Path
from typing import List, Dict, Any, Optional, TypedDict


# ---------------------------------------------------------------------------
# NBAData: 所有 Provider 的统一返回类型
# ---------------------------------------------------------------------------

class NBAData(TypedDict):
    kind: str                       # "boxscore" | "scores_summary" | "standings"
    source: str
    block: str
    matches: List[Dict[str, Any]]
    matches_detail: List[Dict[str, Any]]
    standings: List[Dict[str, Any]]
    was_filtered: bool


def _empty_nba_data(kind: str, source: str = "", block: str = "") -> NBAData:
    """构造空的 NBAData，用于数据不存在时的返回值。"""
    return NBAData(
        kind=kind, source=source, block=block,
        matches=[], matches_detail=[], standings=[], was_filtered=False,
    )


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _get_config_root(key: str) -> Optional[Path]:
    """从 Flask app config 或环境变量读取目录路径，返回 Path 或 None。"""
    try:
        from flask import current_app
        val = current_app.config.get(key, "")
    except Exception:
        import os
        val = os.getenv(key, "")
    if not val:
        return None
    p = Path(val).resolve()
    return p if p.is_dir() else None


# ---------------------------------------------------------------------------
# Team alias table (shared by BoxscoreProvider & ScoresSummaryProvider)
# ---------------------------------------------------------------------------

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
    "猛龙": ["猛龙", "Raptors", "多伦多猛龙", "多伦多"],
    "雷霆": ["雷霆", "Thunder", "俄克拉荷马城雷霆", "俄克拉荷马城"],
    "灰熊": ["灰熊", "Grizzlies", "孟菲斯灰熊", "孟菲斯"],
}


def _team_aliases(team_name: str) -> List[str]:
    """返回某队名的所有匹配用别名（含自身），用于 query 匹配。"""
    if not team_name or not team_name.strip():
        return []
    t = team_name.strip()
    return list(_TEAM_ALIASES.get(t, [t]))


def filter_games_by_query(
    games: List[Dict[str, str]],
    query: str,
) -> Optional[List[str]]:
    """
    根据用户 query 从比赛列表中筛选出匹配的 rel_path（队名别名匹配），
    无匹配时返回 None（表示展示全部）。
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

        if _query_contains_alias(home_aliases) or _query_contains_alias(away_aliases):
            matched.append(g["rel_path"])
    return matched or None


# ---------------------------------------------------------------------------
# NBAQueryRouter: rules first, RouteLLM intent fallback
# ---------------------------------------------------------------------------

_STANDINGS_KEYWORDS = frozenset({
    # 排名直接词
    "排名", "排行", "排第", "standings",
    # 战绩/胜负
    "战绩", "胜负", "胜率", "胜场", "负场", "几胜", "几负",
    # 分区/联盟
    "分区", "东部", "西部", "联盟",
    # 连胜连败（streak 属于赛季纵览）
    "连胜", "连败",
    # 季后赛席位
    "附加赛", "play-in", "垫底", "倒数",
})

_BOXSCORE_KEYWORDS = frozenset({
    # 球员/统计总称
    "球员", "技术统计", "数据", "统计", "表现",
    # 得分相关
    "得分王", "砍", "投篮", "出手", "命中",
    # 三分/罚球
    "三分", "罚球",
    # 篮板/助攻/防守
    "助攻", "篮板", "封盖", "盖帽", "抢断", "失误", "犯规",
    # 综合荣誉
    "两双", "三双",
    # 出场/阵容
    "上场", "出场", "首发", "替补",
    # 比赛节次/时段
    "节次", "上半场", "下半场", "加时",
    # 高阶数据
    "+/-", "正负值", "效率值", "真实命中率",
})

_BOXSCORE_INTENTS = frozenset({"player_stats", "game_detail"})
_STANDINGS_INTENTS = frozenset({"standings"})


class NBAQueryRouter:
    """
    按 query 路由到数据源：规则层（关键词）优先，RouteLLM intent 兜底。
    RouteLLM 在 pipeline 上游已完成意图分类，intent 由调用方透传，不产生额外 LLM 调用。
    """

    @staticmethod
    def route(query: str, intent: Optional[str] = None) -> str:
        q = (query or "").strip().lower()
        # Layer 1: keywords (deterministic, high precision)
        if any(kw in q for kw in _STANDINGS_KEYWORDS):
            return "standings"
        if any(kw in q for kw in _BOXSCORE_KEYWORDS):
            return "boxscore"
        # Layer 2: RouteLLM intent fallback (long-tail)
        if intent in _STANDINGS_INTENTS:
            return "standings"
        if intent in _BOXSCORE_INTENTS:
            return "boxscore"
        return "scores_summary"


# ---------------------------------------------------------------------------
# BoxscoreProvider: parsed_boxscore/{date}/{Away}_vs_{Home}/
# ---------------------------------------------------------------------------

class BoxscoreProvider:
    """读取单场比赛的节次比分与球员技术统计。"""

    @staticmethod
    def read(date: str, query: str) -> NBAData:
        boxroot = _get_config_root("NBA_BOXSCORE_ROOT")
        if not boxroot:
            return ScoresSummaryProvider.read(date, query)

        games = BoxscoreProvider._list_games(boxroot, date)
        if not games:
            return ScoresSummaryProvider.read(date, query)

        paths = filter_games_by_query(games, query or "")
        was_filtered = paths is not None
        data = BoxscoreProvider._read_date(boxroot, date, game_rel_paths=paths)
        if data is None:
            return ScoresSummaryProvider.read(date, query)
        data["was_filtered"] = was_filtered
        return data

    @staticmethod
    def _list_games(boxroot: Path, date_str: str) -> List[Dict[str, str]]:
        """列出指定日期的所有比赛目录（轻量扫描 score.json + 主客队 JSON）。"""
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

    @staticmethod
    def _read_date(
        boxroot: Path,
        date_str: str,
        game_rel_paths: Optional[List[str]] = None,
    ) -> Optional[NBAData]:
        """从 parsed_boxscore/{date}/ 读取比赛详细数据（节次、球员、合计）。"""
        date_dir = boxroot / date_str
        if not date_dir.is_dir():
            return None
        if game_rel_paths is not None:
            game_dirs = [date_dir / p for p in game_rel_paths if (date_dir / p).is_dir()]
            game_dirs.sort(key=lambda d: d.name)
        else:
            game_dirs = sorted(
                (d for d in date_dir.iterdir() if d.is_dir()),
                key=lambda d: d.name,
            )
        if not game_dirs:
            return None

        matches: List[Dict[str, Any]] = []
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

            total_row = rounds_raw[-1] if rounds_raw else {}
            if not isinstance(total_row, dict):
                continue
            team_keys = [k for k in total_row if k != "节次" and total_row.get(k) is not None]
            if len(team_keys) != 2:
                continue
            score_a = total_row.get(team_keys[0])
            score_b = total_row.get(team_keys[1])

            home_team: Optional[str] = None
            away_team: Optional[str] = None
            home_json_path: Optional[Path] = None
            away_json_path: Optional[Path] = None

            for f in game_dir.iterdir():
                if f.suffix != ".json" or f.name == "score.json":
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
                "date": str(game_date), "place": "", "match_time": "",
                "home_team": home_team, "home_score": home_score_val or "",
                "away_team": away_team, "away_score": away_score_val or "",
                "status": status_str, "link": "",
            })

            round_list: List[Dict[str, Any]] = [dict(r) for r in rounds_raw if isinstance(r, dict)]

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
                        "姓名": p.get("姓名", ""), "位置": p.get("位置", ""),
                        "时间": p.get("时间", ""), "得分": p.get("得分", ""),
                        "篮板": p.get("篮板", ""), "助攻": p.get("助攻", ""),
                        "投篮": p.get("投篮", ""), "3分": p.get("3分", ""),
                        "罚球": p.get("罚球", ""), "抢断": p.get("抢断", ""),
                        "封盖": p.get("封盖", ""), "失误": p.get("失误", ""),
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
                "game_id": game_id, "round": round_list,
                "home_team": home_team, "away_team": away_team,
                "home_players": home_players, "away_players": away_players,
                "home_totals": home_totals, "away_totals": away_totals,
            })

        if not matches:
            return None
        return NBAData(
            kind="boxscore", source="parsed_boxscore", block=date_str,
            matches=matches, matches_detail=matches_detail,
            standings=[], was_filtered=False,
        )


# ---------------------------------------------------------------------------
# ScoresSummaryProvider: sohu_nba_block4_scores/{date}/scores.json
# ---------------------------------------------------------------------------

class ScoresSummaryProvider:
    """读取当天全部比赛的比分汇总（快速一览）。"""

    @staticmethod
    def read(date: str, query: str) -> NBAData:
        scores_root = _get_config_root("NBA_SCORES_ROOT")
        if not scores_root:
            return _empty_nba_data("scores_summary", block=date)
        scores_file = scores_root / date / "scores.json"
        if not scores_file.is_file():
            return _empty_nba_data("scores_summary", block=date)
        try:
            raw = json.loads(scores_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return _empty_nba_data("scores_summary", block=date)
        if not isinstance(raw, dict):
            return _empty_nba_data("scores_summary", block=date)

        source = str(raw.get("source", ""))
        block = str(raw.get("block", date))
        raw_matches = raw.get("matches")
        if not isinstance(raw_matches, list):
            return _empty_nba_data("scores_summary", source=source, block=block)

        matches: List[Dict[str, str]] = []
        for m in raw_matches:
            if not isinstance(m, dict):
                continue
            matches.append({
                k: str(m.get(k, "")) if m.get(k) is not None else ""
                for k in ("date", "place", "home_team", "home_score",
                          "away_team", "away_score", "status", "match_time", "link")
            })

        games_for_filter = [
            {"rel_path": str(i), "home_team": m.get("home_team", ""), "away_team": m.get("away_team", "")}
            for i, m in enumerate(matches)
        ]
        filtered_indices = filter_games_by_query(games_for_filter, query or "")
        was_filtered = filtered_indices is not None
        if was_filtered:
            idx_set = set(filtered_indices)  # type: ignore[arg-type]
            matches = [m for i, m in enumerate(matches) if str(i) in idx_set]

        return NBAData(
            kind="scores_summary", source=source, block=block,
            matches=matches, matches_detail=[], standings=[],
            was_filtered=was_filtered,
        )


# ---------------------------------------------------------------------------
# StandingsProvider: sohu_nba_standings/{date}/standings.json
# ---------------------------------------------------------------------------

class StandingsProvider:
    """读取当前赛季东/西部球队排名与胜负战绩。"""

    @staticmethod
    def read(date: str, query: str) -> NBAData:
        standings_root = _get_config_root("NBA_STANDINGS_ROOT")
        if not standings_root:
            return _empty_nba_data("standings", block=date)

        standings_file = standings_root / date / "standings.json"
        if not standings_file.is_file():
            standings_file = StandingsProvider._latest_file(standings_root)
        if not standings_file or not standings_file.is_file():
            return _empty_nba_data("standings", block=date)

        try:
            raw = json.loads(standings_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return _empty_nba_data("standings", block=date)
        if not isinstance(raw, dict):
            return _empty_nba_data("standings", block=date)

        source = str(raw.get("source", ""))
        block = str(raw.get("block", date))
        east = raw.get("east") or []
        west = raw.get("west") or []
        standings: List[Dict[str, Any]] = []
        if east:
            standings.append({"conference": "东部", "teams": east})
        if west:
            standings.append({"conference": "西部", "teams": west})
        return NBAData(
            kind="standings", source=source, block=block,
            matches=[], matches_detail=[], standings=standings,
            was_filtered=False,
        )

    @staticmethod
    def _latest_file(root: Path) -> Optional[Path]:
        """当指定日期不存在时回退到最新的日期子目录。"""
        date_dirs = sorted(
            (d for d in root.iterdir() if d.is_dir()),
            key=lambda d: d.name,
            reverse=True,
        )
        for d in date_dirs:
            f = d / "standings.json"
            if f.is_file():
                return f
        return None


# ---------------------------------------------------------------------------
# list_nba_games_for_date: convenience re-export (used by scores_formatter)
# ---------------------------------------------------------------------------

def list_nba_games_for_date(date_str: str) -> List[Dict[str, str]]:
    """列出指定日期的 NBA 比赛（仅轻量扫描，不读球员详情）。"""
    boxroot = _get_config_root("NBA_BOXSCORE_ROOT")
    if not boxroot:
        return []
    return BoxscoreProvider._list_games(boxroot, date_str)


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------

def query_nba_data(date: str, query: str, intent: Optional[str] = None) -> NBAData:
    """NBA 数据统一查询入口：按 query 语义路由到对应 Provider 读取数据。"""
    kind = NBAQueryRouter.route(query, intent=intent)
    if kind == "standings":
        return StandingsProvider.read(date, query)
    if kind == "boxscore":
        return BoxscoreProvider.read(date, query)
    return ScoresSummaryProvider.read(date, query)
