# app/services/time_intent_classifier.py
"""
时间意图分类器 - TimeIntentClassifier

职责：判断用户问的「昨天」等是指「昨日报道」还是「昨日发生的事」。
独立于 IntentClassifier，在 TemporalResolver 之后调用；
规则前置 + 小模型扩展：规则未命中时调用与意图判断相同的本地小模型（vLLM）。
"""
import re
from typing import Optional
from loguru import logger

from app.services.schemas import TimeIntent


# 新闻/报道/消息类（用于「仅有时间词」时区分 publish vs ambiguous）
_NEWS_REF_PATTERN = re.compile(r"新闻|报道|消息|资讯|快讯|头条", re.I)

# 明确「发布」语义：昨日/昨天/今日 + 发布/的新闻
_PUBLISH_PATTERNS = [
    re.compile(r"昨日\s*发布|昨天\s*发布|今日\s*发布", re.I),
    re.compile(r"(昨日|昨天|今日)\s*的\s*新闻", re.I),
]

# 时间词（用于判断是否涉及时序）
_TIME_WORDS_PATTERN = re.compile(r"昨天|昨日|前天|前日|今天|今日", re.I)

# 事件类表述（赢了吗、打得如何、宣布、比赛、赛果等）
_EVENT_VERBS = [
    "赢", "输", "打", "比赛", "赛果", "比分", "宣布", "达成", "交易",
    "涨", "跌", "收盘", "开盘", "对阵", "大胜", "惜败", "击败",
    "夺冠", "晋级", "淘汰", "签约", "转会", "裁员", "裁员",
]
_EVENT_PATTERN = re.compile("|".join(re.escape(w) for w in _EVENT_VERBS))

_TIME_INTENT_PROMPT = """判断用户查询中的「昨天」「昨日」「前天」「今日」等时间词，是指「昨日报道」（报道发布时间）还是「昨日发生的事」（事件发生时间）。

今日日期：{current_date}
用户查询：{query}

规则：
- publish_time：用户明确在问「昨天/今日 发布/报道 的新闻」，关注的是稿件发布时间
- event_time：用户问的是「昨天发生的事」「昨天比赛结果」「昨天价格」等，关注的是事件本身发生的时间

只输出一个词：publish_time 或 event_time，不要解释。"""


class TimeIntentClassifier:
    """
    时间意图分类器（规则前置 + 小模型扩展）。
    与 IntentClassifier 共用同一本地小模型（get_local_llm_service）。
    规则未命中或返回 ambiguous 时调用 LLM；LLM 不可用则 fallback 为 event_time。
    """

    def __init__(self, local_llm_service=None):
        self._local_llm = local_llm_service

    @property
    def local_llm(self):
        if self._local_llm is None:
            from app.services.local_llm_service import get_local_llm_service
            self._local_llm = get_local_llm_service()
        return self._local_llm

    def _classify_with_llm(self, query: str, reference_date: Optional[str], current_date: str) -> TimeIntent:
        """规则未命中时，用与意图判断相同的小模型做时间意图分类。"""
        if not self.local_llm.is_available:
            logger.warning("[TimeIntentClassifier] 本地模型不可用，fallback 为 event_time")
            return TimeIntent(time_reference_type="event_time")
        prompt = _TIME_INTENT_PROMPT.format(
            current_date=current_date,
            query=query,
        )
        try:
            raw = self.local_llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=16,
            ).strip().lower()
            if "publish_time" in raw or "publish" in raw:
                return TimeIntent(time_reference_type="publish_time")
            if "event_time" in raw or "event" in raw:
                return TimeIntent(time_reference_type="event_time")
            logger.debug(f"[TimeIntentClassifier] LLM 输出无法解析，fallback event_time: {raw}")
            return TimeIntent(time_reference_type="event_time")
        except Exception as e:
            logger.warning(f"[TimeIntentClassifier] LLM 调用失败: {e}，fallback event_time")
            return TimeIntent(time_reference_type="event_time")

    def classify(
        self,
        query: str,
        reference_date: Optional[str] = None,
        current_date: Optional[str] = None,
    ) -> TimeIntent:
        """
        分类时间意图。规则前置，未命中时调小模型。

        Args:
            query: 用户查询（或 standalone_query）
            reference_date: 已解析的目标日期 YYYY-MM-DD（可选）
            current_date: 今日日期 YYYY-MM-DD（LLM prompt 用，可选）

        Returns:
            TimeIntent: time_reference_type in ("publish_time", "event_time", "ambiguous")
        """
        from datetime import datetime
        cur = current_date or datetime.now().strftime("%Y-%m-%d")
        q = (query or "").strip()
        if not q:
            return TimeIntent(time_reference_type="ambiguous")

        # 1. 明确「发布」语义 -> publish_time
        for pat in _PUBLISH_PATTERNS:
            if pat.search(q):
                return TimeIntent(time_reference_type="publish_time")

        # 2. 有时间词 + 事件类表述 -> event_time
        if _TIME_WORDS_PATTERN.search(q) and _EVENT_PATTERN.search(q):
            return TimeIntent(time_reference_type="event_time")

        # 3. 仅有时间词（无「发布」、无事件动词）
        if _TIME_WORDS_PATTERN.search(q):
            if _NEWS_REF_PATTERN.search(q):
                return TimeIntent(time_reference_type="publish_time")
            return TimeIntent(time_reference_type="ambiguous")

        # 4. 无上述时间词 -> ambiguous（此时通常不调用本分类器，router 按 publish_time_only 保守处理）
        return TimeIntent(time_reference_type="ambiguous")

    # 扩展点：可增加 _rule_first_then_llm 等策略


def get_time_intent_classifier(local_llm_service=None) -> TimeIntentClassifier:
    """获取 TimeIntentClassifier 实例（可注入 local_llm_service，与 IntentClassifier 共用）"""
    return TimeIntentClassifier(local_llm_service=local_llm_service)
