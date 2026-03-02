# app/services/route_llm.py
"""
路由小 LLM - 仅根据当前用户句 + 上轮 filter_category 输出显式意图维度
输出 need_retrieval / need_scores，由 Router 推导 action，编排层决定检索与赛况是否混合。
"""
import os
from typing import Optional
from loguru import logger

from app.services.schemas import RouteLLMOutput, _action_from_intent
from app.services.local_llm_service import get_local_llm_service


_ROUTE_SYSTEM = """根据「当前用户句」和「上轮类别」输出显式意图维度（仅两个布尔值 + 类别/时效）。

need_retrieval：是否需要检索（向量库/新闻）。要报道、资讯、动态、发生了什么等填 true；仅要比分/赛果且不需报道时填 false。
need_scores：是否需要赛况数据引擎（比分/赛果/打得怎么样）。明确涉及比分、赛果、赛况、几比几、打得怎么样时填 true；只要新闻/资讯或明确说不要赛况/不要比分时填 false。
两者独立：可仅检索(need_retrieval=true, need_scores=false)、仅赛况(need_retrieval=false, need_scores=true)、或混合(两者都 true)。体育类 + 新闻问法（如「有什么NBA新闻」「近一个月NBA新闻」）为 need_retrieval=true, need_scores=false。

原则：上轮类别有值且当前句是短追问（再详细点、有没有细节、更多、具体点）：继承上轮类别，need_retrieval 按当前句是否要检索填。上轮类别为「无」或当前句为新话题时，按当前句内容判断类别与 need_retrieval/need_scores。

追问时还需输出 follow_up_time_type（仅上轮类别有值且当前为追问时填，否则 null）：
- time_switch：当前句含明确时间词（今天、明天、3月1日等）；
- event_continue：用户要求更多细节（再详细点、有没有更多、具体点）；
- object_switch：用户问另一对象/队伍（勇士呢、湖人呢）。
只输出一行 JSON，无解释。"""

# 缩写版：模型 context 较小时使用（如 max_length=864），控制 system+user 总 token
_ROUTE_SYSTEM_SHORT = """按当前用户句+上轮类别输出JSON。need_retrieval=要检索报道填true；need_scores=要比分/赛况填true，只要新闻或不要赛况填false。follow_up_time_type: 追问含时间词填time_switch，要更多细节填event_continue，换对象填object_switch，否则null。只输出一行JSON。"""

# 精简 few-shot（约 4 条）供小 context 模型使用，保证 prompt 不超长
_FEW_SHOT_MINIMAL = [
    ("再详细点", "sports", '{"need_retrieval":true,"need_scores":true,"filter_category":"sports","time_sensitivity":"recent","follow_up_time_type":"event_continue"}'),
    ("有没有新闻不要赛况", "sports", '{"need_retrieval":true,"need_scores":false,"filter_category":"sports","time_sensitivity":"recent","follow_up_time_type":"event_continue"}'),
    ("今天76人打得怎么样", "无", '{"need_retrieval":true,"need_scores":true,"filter_category":"sports","time_sensitivity":"recent"}'),
    ("近一个月NBA新闻", "无", '{"need_retrieval":true,"need_scores":false,"filter_category":"sports","time_sensitivity":"recent"}'),
    ("你好", "无", '{"need_retrieval":false,"need_scores":false,"filter_category":"general","time_sensitivity":"none"}'),
]

_FEW_SHOT_USER = """用户：「{user}」
上轮类别：{last_cat}

输出 JSON："""

# 紧凑单行格式，用于缩写策略
_FEW_SHOT_LINE = "用户:{user} 上轮:{last_cat} -> {out}"

# 模型最大 context（token），超过则用缩写 prompt；可从环境 LOCAL_LLM_MAX_CONTEXT_TOKENS 覆盖，默认 864
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
        lines = [
            _FEW_SHOT_LINE.format(user=u, last_cat=c, out=o)
            for u, c, o in examples
        ]
        return "\n".join(lines)
    lines = []
    for user, last_cat, out in examples:
        lines.append(_FEW_SHOT_USER.format(user=user, last_cat=last_cat).strip())
        lines.append(out)
    return "\n".join(lines)


_FEW_SHOT_EXAMPLES = [
    ("再详细点", "sports", '{"need_retrieval":true,"need_scores":true,"filter_category":"sports","time_sensitivity":"recent","follow_up_time_type":"event_continue"}'),
    ("有没有新闻，不要赛况", "sports", '{"need_retrieval":true,"need_scores":false,"filter_category":"sports","time_sensitivity":"recent","follow_up_time_type":"event_continue"}'),
    ("有没有细节", "economy", '{"need_retrieval":true,"need_scores":false,"filter_category":"economy","time_sensitivity":"recent","follow_up_time_type":"event_continue"}'),
    ("更多数据", "tech", '{"need_retrieval":true,"need_scores":false,"filter_category":"tech","time_sensitivity":"recent","follow_up_time_type":"event_continue"}'),
    ("勇士呢", "sports", '{"need_retrieval":true,"need_scores":true,"filter_category":"sports","time_sensitivity":"recent","follow_up_time_type":"object_switch"}'),
    ("那今天呢", "sports", '{"need_retrieval":true,"need_scores":true,"filter_category":"sports","time_sensitivity":"recent","follow_up_time_type":"time_switch"}'),
    ("今天76人打得怎么样", "无", '{"need_retrieval":true,"need_scores":true,"filter_category":"sports","time_sensitivity":"recent"}'),
    ("近一个月有什么NBA的新闻", "无", '{"need_retrieval":true,"need_scores":false,"filter_category":"sports","time_sensitivity":"recent"}'),
    ("今天NBA比分", "无", '{"need_retrieval":false,"need_scores":true,"filter_category":"sports","time_sensitivity":"recent"}'),
    ("黄金现在多少钱", "无", '{"need_retrieval":true,"need_scores":false,"filter_category":"economy","time_sensitivity":"realtime"}'),
    ("苹果最近有什么新品", "无", '{"need_retrieval":true,"need_scores":false,"filter_category":"tech","time_sensitivity":"recent"}'),
    ("北京明天天气怎么样", "无", '{"need_retrieval":false,"need_scores":false,"filter_category":"general","time_sensitivity":"none"}'),
    ("你好", "无", '{"need_retrieval":false,"need_scores":false,"filter_category":"general","time_sensitivity":"none"}'),
]


def _build_user_message(current_utterance: str, last_filter_category: Optional[str], abbreviated: bool = False) -> str:
    last_cat = last_filter_category if last_filter_category else "无"
    block = _build_few_shot_block(abbreviated=abbreviated)
    if abbreviated:
        return f"{block}\n用户:{current_utterance} 上轮:{last_cat} ->"
    return f"""{block}

用户：「{current_utterance}」
上轮类别：{last_cat}

输出 JSON："""


class RouteLLM:
    """路由小 LLM：输入仅当前用户句 + 上轮 filter_category，输出 need_retrieval / need_scores 等显式意图。"""

    def __init__(self, local_llm_service=None):
        self._local_llm = local_llm_service

    @property
    def local_llm(self):
        if self._local_llm is None:
            self._local_llm = get_local_llm_service()
        return self._local_llm

    def invoke(
        self,
        current_user_utterance: str,
        last_filter_category: Optional[str] = None,
    ) -> RouteLLMOutput:
        """
        单次调用：根据当前用户句和上轮类别输出显式意图（need_retrieval, need_scores）。
        失败则抛错（不兜底）。
        """
        user_content_full = _build_user_message(
            current_user_utterance.strip(),
            last_filter_category,
            abbreviated=False,
        )
        use_short = _use_abbreviated_prompt(_ROUTE_SYSTEM, user_content_full, max_tokens=256)
        if use_short:
            system_content = _ROUTE_SYSTEM_SHORT
            user_content = _build_user_message(
                current_user_utterance.strip(),
                last_filter_category,
                abbreviated=True,
            )
            logger.debug("RouteLLM 使用缩写 prompt（估计 token 超限）")
        else:
            system_content = _ROUTE_SYSTEM
            user_content = user_content_full
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]
        try:
            result = self.local_llm.chat_with_schema(
                messages=messages,
                response_schema=RouteLLMOutput,
                temperature=0.0,
                max_tokens=256,
            )
            action = _action_from_intent(result.need_retrieval, result.need_scores)
            logger.debug(
                "RouteLLM output: need_retrieval=%s need_scores=%s -> action=%s filter_category=%s follow_up_time_type=%s",
                result.need_retrieval,
                result.need_scores,
                action,
                result.filter_category,
                getattr(result, "follow_up_time_type", None),
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
