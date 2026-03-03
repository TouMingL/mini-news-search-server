# app/services/route_llm.py
"""
路由小 LLM（Query Parser 架构）
职责分离：LLM 做结构化理解（实体提取 + 意图识别），规则层做确定性路由推导。
下游 Router / Pipeline 仍消费 RouteLLMOutput，零改动。
"""
import os
from typing import Optional
from loguru import logger

from app.services.schemas import (
    RouteLLMOutput,
    QueryParseResult,
    _action_from_intent,
)
from app.services.local_llm_service import get_local_llm_service


# ============ 规则层：QueryParseResult → RouteLLMOutput ============

_SCORES_INTENTS = {"scores", "player_stats", "game_detail", "standings"}


def derive_route_output(parsed: QueryParseResult) -> RouteLLMOutput:
    """确定性规则：由结构化理解推导路由布尔值，消除 LLM 对分词/空格的敏感性。"""
    has_sports_entity = any(
        e.type in ("team", "player", "league", "sport_type")
        for e in parsed.entities
    )
    is_scores_intent = parsed.intent in _SCORES_INTENTS

    need_scores = (
        parsed.category == "sports"
        and (is_scores_intent or has_sports_entity)
    )
    need_retrieval = parsed.intent in (
        "news", "general_query", "realtime_quote", "game_detail", "player_stats",
    )
    if parsed.intent == "chitchat":
        need_retrieval = False
        need_scores = False

    return RouteLLMOutput(
        need_retrieval=need_retrieval,
        need_scores=need_scores,
        filter_category=parsed.category,
        time_sensitivity=parsed.time_sensitivity,
        follow_up_time_type=parsed.follow_up_type,
    )


# ============ Parser LLM Prompt ============

_PARSER_SYSTEM = """你是查询解析器。从用户句中提取实体、识别意图、判断类别。

## 实体类型（entities 数组，每个元素含 type 和 value）
- team：球队名（森林狼、湖人、Heat、活塞、掘金等）
- player：球员名（安东尼、詹姆斯、库里、爱德华兹等）
- league：赛事/联赛（NBA、CBA、英超、WTT、欧冠等）
- sport_type：运动类型（篮球、足球、乒乓球等）
- financial：金融品种（黄金、白银、原油等）
- time：时间表达（今天、3月2日、上周六等）
- location：地点（北京、洛杉矶等）
- person：其他人物
- org：其他组织
- other：其他实体
无实体时 entities 为空数组。

## 意图类型（intent，单选）
- scores：要比分/赛果/几比几
- player_stats：要球员数据/详细统计
- game_detail：要比赛详情/打得怎么样/比赛细节
- standings：要排名/战绩/胜负/分区排名
- news：要新闻/报道/动态/发生了什么
- realtime_quote：要实时行情/价格
- general_query：一般信息查询
- chitchat：闲聊/问候

## 类别（category，单选）
sports / economy / tech / world / health / academic / general

## 时效性（time_sensitivity，单选）
- realtime：需要实时数据（行情、价格）
- recent：近期事件/新闻
- historical：历史信息
- none：无时效要求

## 追问类型（follow_up_type，仅上轮有类别且当前为追问时填，否则 null）
- time_switch：当前句含明确时间词（今天、明天、3月1日等）
- event_continue：用户要求更多细节（再详细点、具体点、展开讲讲）
- object_switch：用户问另一对象/队伍（勇士呢、湖人呢）

只输出一行 JSON，无解释。"""

_PARSER_SYSTEM_SHORT = """查询解析器。提取entities(type:team/player/league/sport_type/financial/time/location/person/org/other,value)、intent(scores/player_stats/game_detail/standings/news/realtime_quote/general_query/chitchat)、category(sports/economy/tech/world/health/academic/general)、time_sensitivity(realtime/recent/historical/none)、follow_up_type(time_switch/event_continue/object_switch/null)。只输出JSON。"""

_FEW_SHOT_EXAMPLES = [
    ("昨天湖人詹姆斯数据", "无",
     '{"entities":[{"type":"time","value":"昨天"},{"type":"team","value":"湖人"},{"type":"player","value":"詹姆斯"}],"intent":"player_stats","category":"sports","time_sensitivity":"recent","follow_up_type":null}'),
    ("再详细点", "sports",
     '{"entities":[],"intent":"game_detail","category":"sports","time_sensitivity":"recent","follow_up_type":"event_continue"}'),
    ("掘金呢", "sports",
     '{"entities":[{"type":"team","value":"掘金"}],"intent":"game_detail","category":"sports","time_sensitivity":"recent","follow_up_type":"object_switch"}'),
    ("那明天呢", "sports",
     '{"entities":[{"type":"time","value":"明天"}],"intent":"game_detail","category":"sports","time_sensitivity":"recent","follow_up_type":"time_switch"}'),
    ("最近英超有什么新闻", "无",
     '{"entities":[{"type":"time","value":"最近"},{"type":"league","value":"英超"}],"intent":"news","category":"sports","time_sensitivity":"recent","follow_up_type":null}'),
    ("东部排名", "无",
     '{"entities":[{"type":"league","value":"NBA"}],"intent":"standings","category":"sports","time_sensitivity":"recent","follow_up_type":null}'),
    ("白银现在什么价", "无",
     '{"entities":[{"type":"financial","value":"白银"}],"intent":"realtime_quote","category":"economy","time_sensitivity":"realtime","follow_up_type":null}'),
    ("你好", "无",
     '{"entities":[],"intent":"chitchat","category":"general","time_sensitivity":"none","follow_up_type":null}'),
]

_FEW_SHOT_MINIMAL = [
    ("昨天湖人詹姆斯数据", "无",
     '{"entities":[{"type":"time","value":"昨天"},{"type":"team","value":"湖人"},{"type":"player","value":"詹姆斯"}],"intent":"player_stats","category":"sports","time_sensitivity":"recent","follow_up_type":null}'),
    ("再详细点", "sports",
     '{"entities":[],"intent":"game_detail","category":"sports","time_sensitivity":"recent","follow_up_type":"event_continue"}'),
    ("东部排名", "无",
     '{"entities":[{"type":"league","value":"NBA"}],"intent":"standings","category":"sports","time_sensitivity":"recent","follow_up_type":null}'),
    ("白银现在什么价", "无",
     '{"entities":[{"type":"financial","value":"白银"}],"intent":"realtime_quote","category":"economy","time_sensitivity":"realtime","follow_up_type":null}'),
    ("你好", "无",
     '{"entities":[],"intent":"chitchat","category":"general","time_sensitivity":"none","follow_up_type":null}'),
]

_FEW_SHOT_USER = """用户：「{user}」
上轮类别：{last_cat}

输出 JSON："""

_FEW_SHOT_LINE = "用户:{user} 上轮:{last_cat} -> {out}"


# ============ Prompt 构建工具 ============

def _estimate_tokens(text: str) -> int:
    """粗略估计字符对应 token 数（中英混合约 1.2~1.5 字/token）。"""
    if not text:
        return 0
    return max(1, int(len(text) / 1.3))


def _get_max_context_tokens() -> int:
    try:
        from flask import current_app
        v = current_app.config.get("LOCAL_LLM_MAX_CONTEXT_TOKENS")
        if v is not None:
            return int(v)
    except RuntimeError:
        pass
    return int(os.environ.get("LOCAL_LLM_MAX_CONTEXT_TOKENS", "864"))


def _use_abbreviated_prompt(system: str, user_content: str, max_tokens: int = 256) -> bool:
    """是否使用缩写版 prompt：system+user 估计 token 数超过 (模型上限 - max_tokens) 时用缩写。"""
    limit = _get_max_context_tokens()
    total_input = _estimate_tokens(system) + _estimate_tokens(user_content)
    return total_input + max_tokens > limit


def _build_few_shot_block(abbreviated: bool = False) -> str:
    examples = _FEW_SHOT_MINIMAL if abbreviated else _FEW_SHOT_EXAMPLES
    if abbreviated:
        return "\n".join(
            _FEW_SHOT_LINE.format(user=u, last_cat=c, out=o)
            for u, c, o in examples
        )
    lines = []
    for user, last_cat, out in examples:
        lines.append(_FEW_SHOT_USER.format(user=user, last_cat=last_cat).strip())
        lines.append(out)
    return "\n".join(lines)


def _build_user_message(current_utterance: str, last_filter_category: Optional[str], abbreviated: bool = False) -> str:
    last_cat = last_filter_category if last_filter_category else "无"
    block = _build_few_shot_block(abbreviated=abbreviated)
    if abbreviated:
        return f"{block}\n用户:{current_utterance} 上轮:{last_cat} ->"
    return f"""{block}

用户：「{current_utterance}」
上轮类别：{last_cat}

输出 JSON："""


# ============ RouteLLM 类 ============

class RouteLLM:
    """
    路由 LLM（Query Parser 架构）：
    LLM 输出结构化理解 QueryParseResult → 规则层 derive_route_output() 推导 RouteLLMOutput。
    对外接口不变，返回 RouteLLMOutput。
    """

    def __init__(self, local_llm_service=None):
        self._local_llm = local_llm_service
        self._last_parse_result: Optional[QueryParseResult] = None

    @property
    def local_llm(self):
        if self._local_llm is None:
            self._local_llm = get_local_llm_service()
        return self._local_llm

    @property
    def last_parse_result(self) -> Optional[QueryParseResult]:
        """最近一次调用的 QueryParseResult，供 tracer 记录。"""
        return self._last_parse_result

    def invoke(
        self,
        current_user_utterance: str,
        last_filter_category: Optional[str] = None,
    ) -> RouteLLMOutput:
        """
        Parser LLM 提取实体+意图 → 规则层推导路由布尔值 → 返回 RouteLLMOutput。
        """
        self._last_parse_result = None
        user_content_full = _build_user_message(
            current_user_utterance.strip(),
            last_filter_category,
            abbreviated=False,
        )
        use_short = _use_abbreviated_prompt(_PARSER_SYSTEM, user_content_full, max_tokens=256)
        if use_short:
            system_content = _PARSER_SYSTEM_SHORT
            user_content = _build_user_message(
                current_user_utterance.strip(),
                last_filter_category,
                abbreviated=True,
            )
            logger.debug("RouteLLM 使用缩写 prompt（估计 token 超限）")
        else:
            system_content = _PARSER_SYSTEM
            user_content = user_content_full
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]
        try:
            parsed = self.local_llm.chat_with_schema(
                messages=messages,
                response_schema=QueryParseResult,
                temperature=0.0,
                max_tokens=256,
            )
            self._last_parse_result = parsed
            result = derive_route_output(parsed)
            action = _action_from_intent(result.need_retrieval, result.need_scores)
            logger.debug(
                "QueryParser entities=%s intent=%s -> need_retrieval=%s need_scores=%s action=%s",
                [(e.type, e.value) for e in parsed.entities],
                parsed.intent,
                result.need_retrieval,
                result.need_scores,
                action,
            )
            return result
        except Exception as e:
            logger.error("RouteLLM 调用失败: %s", e)
            raise


_route_llm_instance: Optional[RouteLLM] = None


def get_route_llm(local_llm_service=None) -> RouteLLM:
    """获取 RouteLLM 单例"""
    global _route_llm_instance
    if _route_llm_instance is None:
        _route_llm_instance = RouteLLM(local_llm_service=local_llm_service)
    return _route_llm_instance
