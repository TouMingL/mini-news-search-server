# app/services/answer_verifier.py
"""
回答校验模块：生成与校验分离，单一职责。

职责边界：
- 捏造检测：回答中的事实是否均能在检索结果中找到依据。
- 扣题检测：回答是否针对用户问题（含「否定回答」语义：如「有X吗」->「没有X，目前有Y」视为扣题）。
- 时间对齐：回答中的时间表述是否与 reference_date 一致；对「否定/当前态」回答采用语义策略，不强制显式日期。

校验结果带失败原因，便于 Pipeline 做替换决策与 Tracer 可观测。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from loguru import logger


class VerifyFailureReason(str, Enum):
    """校验不通过时的原因，用于替换文案与 trace。"""
    FABRICATION = "fabrication"
    OFF_TOPIC = "off_topic"
    TIME_CONSISTENCY = "time_consistency"


@dataclass
class VerificationResult:
    """校验结果：是否通过 + 若不通过则带原因 + 各步耗时（供 log/trace）。"""
    passed: bool
    failure_reason: Optional[VerifyFailureReason] = None
    fabrication_ms: Optional[float] = None
    on_topic_ms: Optional[float] = None
    temporal_ms: Optional[float] = None

    @classmethod
    def ok(
        cls,
        fabrication_ms: Optional[float] = None,
        on_topic_ms: Optional[float] = None,
        temporal_ms: Optional[float] = None,
    ) -> VerificationResult:
        return cls(
            passed=True,
            failure_reason=None,
            fabrication_ms=fabrication_ms,
            on_topic_ms=on_topic_ms,
            temporal_ms=temporal_ms,
        )

    @classmethod
    def fail(
        cls,
        reason: VerifyFailureReason,
        fabrication_ms: Optional[float] = None,
        on_topic_ms: Optional[float] = None,
        temporal_ms: Optional[float] = None,
    ) -> VerificationResult:
        return cls(
            passed=False,
            failure_reason=reason,
            fabrication_ms=fabrication_ms,
            on_topic_ms=on_topic_ms,
            temporal_ms=temporal_ms,
        )


# 拒答时的用户可见文案（按原因区分，便于后续扩展或 i18n）
REPLACEMENT_MESSAGES: Dict[VerifyFailureReason, str] = {
    VerifyFailureReason.FABRICATION: "根据当前检索到的内容无法可靠回答该问题，请稍后重试或换个问法。",
    VerifyFailureReason.OFF_TOPIC: "根据当前检索到的内容无法可靠回答该问题，请稍后重试或换个问法。",
    VerifyFailureReason.TIME_CONSISTENCY: "根据当前检索到的内容无法可靠回答该问题，请稍后重试或换个问法。",
}

# 时间证据缺失（在生成前由 Pipeline 判断，不属于 verify 结果）
NO_EVIDENCE_FOR_DATE_MESSAGE = "当前检索结果中未找到该日期的报道，无法据此作答。"


def _normalize_reference_date(ref: Optional[str]) -> Optional[str]:
    """将 reference_date 规范为 YYYY-MM-DD；无效则返回 None。"""
    if not ref or not isinstance(ref, str):
        return None
    ref = ref.strip()
    if len(ref) == 10 and ref[4] == "-" and ref[7] == "-":
        try:
            datetime.strptime(ref, "%Y-%m-%d")
            return ref
        except ValueError:
            pass
    m = re.match(r"(\d{1,2})月(\d{1,2})日", ref)
    if m:
        try:
            y = datetime.now().year
            dt = datetime(y, int(m.group(1)), int(m.group(2)))
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _parse_rel_date_in_text(text: str, current_date: str) -> Optional[str]:
    """从文本解析「昨日」「今天」等为 YYYY-MM-DD。"""
    if not text or not current_date:
        return None
    lower = text.strip().lower()
    try:
        base = datetime.strptime(current_date, "%Y-%m-%d")
    except ValueError:
        return None
    from datetime import timedelta
    if "今日" in lower or "今天" in lower:
        return current_date
    if "昨日" in lower or "昨天" in lower:
        return (base - timedelta(days=1)).strftime("%Y-%m-%d")
    if "前天" in lower or "前日" in lower:
        return (base - timedelta(days=2)).strftime("%Y-%m-%d")
    return None


# 回答中显式日期
_ANSWER_DATE_RE = re.compile(
    r'(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日|(\d{4})-(\d{1,2})-(\d{1,2})'
)
# 相对日期词
_ANSWER_REL_DATE_RE = re.compile(r'昨日|昨天|前天|前日|今天|今日')
# 否定/当前态表述：用于语义时间对齐（表示「当前没有/正在进行」时可视为与「今天」一致）
_CURRENT_OR_NEGATIVE_RE = re.compile(
    r'目前(没有|无)|暂无|没有.*(已)?结束|进行中|当前|目前(进行|在)'
)
# 正面赛果表述：回答中描述具体比赛结果时，仅靠「今天/刚刚」等相对词不足以证明与 reference_date 一致，须有显式日历日期
_POSITIVE_EVENT_CLAIM_RE = re.compile(
    r'战胜|击败|(?:以|比)?\d{1,3}[-–]\d{1,3}(?:战胜|取胜|负于)?|刚结束|刚刚.*(?:战胜|击败)'
)
# 进行中/当前比分：含此类表述时视为「当前态」播报，即使用到 X-Y 比分也不按「已结束赛果」要求显式日期
_LIVE_OR_CURRENT_SCORE_RE = re.compile(
    r'进行中|目前比分为?|目前.*领先|仍在进行|比赛仍在进行'
)


def get_replacement_message(reason: VerifyFailureReason) -> str:
    """根据失败原因返回替换文案。"""
    return REPLACEMENT_MESSAGES.get(reason, REPLACEMENT_MESSAGES[VerifyFailureReason.FABRICATION])


class AnswerVerifier:
    """
    回答校验器：捏造、扣题、时间对齐三项分工，返回结构化结果与失败原因。
    依赖 LLM 仅用于捏造/扣题两类判断；时间对齐为规则+语义策略，无额外 LLM 调用。
    """

    def __init__(self, llm_service: Any):
        """
        llm_service: 需提供 chat(messages, temperature=..., max_tokens=...) 方法，用于捏造与扣题检测。
        """
        self._llm = llm_service

    def verify(
        self,
        query: str,
        answer: str,
        context: List[Dict],
        reference_date: Optional[str] = None,
        current_date: Optional[str] = None,
    ) -> VerificationResult:
        """
        按顺序执行：捏造 -> 扣题 -> 时间对齐。任一项不通过即返回对应原因。
        reference_date 为回答范围日期（answer_scope_date），由 pipeline 从 temporal_scope 推导传入；有值时才做时间对齐。
        """
        if not answer or not answer.strip():
            return VerificationResult.fail(VerifyFailureReason.OFF_TOPIC)

        t0 = time.perf_counter()
        fabrication_ok = self._verify_no_fabrication(context, answer)
        fabrication_ms = (time.perf_counter() - t0) * 1000
        logger.info("事实核查-捏造: {} | 耗时 {:.1f}ms", "通过" if fabrication_ok else "不通过", fabrication_ms)
        if not fabrication_ok:
            logger.info("事实核查不通过：捏造")
            return VerificationResult.fail(VerifyFailureReason.FABRICATION, fabrication_ms=fabrication_ms)

        t0 = time.perf_counter()
        on_topic_ok = self._verify_on_topic(query, answer)
        on_topic_ms = (time.perf_counter() - t0) * 1000
        logger.info("事实核查-答非所问: {} | 耗时 {:.1f}ms", "通过" if on_topic_ok else "不通过", on_topic_ms)
        if not on_topic_ok:
            logger.info("事实核查不通过：答非所问")
            return VerificationResult.fail(
                VerifyFailureReason.OFF_TOPIC,
                fabrication_ms=fabrication_ms,
                on_topic_ms=on_topic_ms,
            )

        t0 = time.perf_counter()
        time_ok = self._verify_temporal_alignment(answer, reference_date, current_date)
        temporal_ms = (time.perf_counter() - t0) * 1000
        logger.info("事实核查-时间对齐: {} | 耗时 {:.1f}ms", "通过" if time_ok else "不通过", temporal_ms)
        if not time_ok:
            logger.info("事实核查不通过：时间一致性")
            return VerificationResult.fail(
                VerifyFailureReason.TIME_CONSISTENCY,
                fabrication_ms=fabrication_ms,
                on_topic_ms=on_topic_ms,
                temporal_ms=temporal_ms,
            )

        total_ms = fabrication_ms + on_topic_ms + temporal_ms
        logger.info("事实核查: 捏造=通过, 答非所问=通过, 时间对齐=通过 | 总耗时 {:.1f}ms", total_ms)
        return VerificationResult.ok(
            fabrication_ms=fabrication_ms,
            on_topic_ms=on_topic_ms,
            temporal_ms=temporal_ms,
        )

    def _verify_no_fabrication(self, context: List[Dict], answer: str) -> bool:
        """捏造检测：回答中的事实是否均能在检索结果中找到依据。来源为赛况数据引擎的条目给予更长截断，主要作格式/一致性校验。"""
        lines = []
        for i, item in enumerate(context, 1):
            src = item.get("source", "")
            title = item.get("title", "")
            raw = item.get("content") or ""
            cap = 2000 if src == "赛况数据引擎" else 300
            content = raw[:cap] if cap else raw
            if len(raw) > cap:
                content = content + "…（已截断）"
            lines.append(f"{i}. 来源：{src} 标题：{title} 内容摘要：{content}")
        context_block = "\n".join(lines) if lines else "（无）"
        user_content = f"""你是事实核查员，只做一件事：判断「回答」中的事实是否都能在「检索到的新闻」中找到依据。

规则：
- 通过：回答中的事件、数据、来源名称等均能在检索到的新闻中找到对应或合理归纳。
- 不通过：回答中出现了检索结果里完全不存在的新闻事件、具体数据或来源名称（凭空编造）。
- 不通过：若检索内容中某场比赛标注为「进行中」，但回答中对该场使用了「获胜」「击败」「取胜」「险胜」等表示已结束的措辞，则判不通过（将进行中当作已结束即属捏造）。

重要：本步骤只判断有无捏造，不判断是否扣题。即使用户问的是A而回答讲的是B，只要回答里的每一条事实（事件、数据、来源）都能在下方检索结果中找到，仍应判「通过」；仅当回答中出现检索结果中不存在的事件/数据/来源时判「不通过」。

检索到的新闻：
{context_block}

回答：
{answer}

只输出「通过」或「不通过」。"""
        try:
            out = (self._llm.chat([{"role": "user", "content": user_content}], temperature=0.1, max_tokens=50)) or ""
            out = out.strip().replace(" ", "")
            passed = "不通过" not in out
            return passed
        except Exception as e:
            logger.warning("捏造检测调用失败，视为通过: {}", e)
            return True

    def _verify_on_topic(self, query: str, answer: str) -> bool:
        """扣题检测：回答是否针对用户问题。显式支持「否定回答」语义。"""
        user_content = f"""你是事实核查员，只做一件事：判断「回答」是否针对「用户问题」作答（未答非所问）。

规则（仅根据用户问题与回答内容判断，不参考任何检索结果）：
- 通过：用户问的是宽泛主题（如「NBA赛况」「黄金行情」），回答是该主题下的具体内容（如某场比赛、某条报价）。
- 通过：用户问的是具体主体（如某支球队、某品种），回答围绕该主体展开（如问火箭、回答中有火箭相关事实）。
- 通过：用户问「有X吗」「有没有X」「X呢」时，回答「没有X」「暂无X」「目前没有X」或「没有X，但有/目前有Y」等否定或替代信息，均视为扣题。
- 不通过：用户问的是具体主体，回答却主要讲其他主体且未紧扣所问（如问火箭比赛近况，回答通篇只有骑士/热火等与火箭无关的内容）。

不判断回答有无编造，只判断是否扣题。回答中只要包含与用户所问主题直接相关的内容（含否定/替代说明）即视为扣题。

用户问题：{query}

回答：{answer}

只输出「通过」或「不通过」。"""
        try:
            out = (self._llm.chat([{"role": "user", "content": user_content}], temperature=0.1, max_tokens=50)) or ""
            out = out.strip().replace(" ", "")
            passed = "不通过" not in out
            return passed
        except Exception as e:
            logger.warning("答非所问检测调用失败，视为通过: {}", e)
            return True

    def _verify_temporal_alignment(
        self,
        answer: str,
        reference_date: Optional[str],
        current_date: Optional[str],
    ) -> bool:
        """
        时间对齐：回答中的时间表述与 reference_date 一致。
        策略：
        - 无 reference_date -> 通过。
        - 回答含显式日期或「昨日/今天」等 -> 与 reference_date 比较，不一致则不过。
        - 回答无显式日期但为「否定/当前态」表述（如「目前没有」「进行中」）且 reference_date 为今天 -> 通过。
        - 回答无任何可对齐日期且非上述当前态 -> 不通过。
        """
        if not reference_date or not answer:
            return True
        canon = _normalize_reference_date(reference_date)
        if not canon:
            return True
        try:
            ref_dt = datetime.strptime(canon, "%Y-%m-%d")
        except ValueError:
            return True

        found_date = False
        found_date_explicit = False
        found_date_relative = False

        # 1. 显式日期
        for m in _ANSWER_DATE_RE.finditer(answer):
            found_date = True
            found_date_explicit = True
            g = m.groups()
            if g[0] is not None and g[1] is not None and g[2] is not None:
                y, mo, d = int(g[0] or ref_dt.year), int(g[1]), int(g[2])
            elif g[3] is not None and g[4] is not None and g[5] is not None:
                y, mo, d = int(g[3]), int(g[4]), int(g[5])
            else:
                continue
            try:
                ans_dt = datetime(y, mo, d)
                if ans_dt.date() != ref_dt.date():
                    logger.info("回答日期与目标日期不一致: 回答中 {} vs reference_date {}", ans_dt.date(), canon)
                    return False
            except ValueError:
                continue

        # 2. 相对日期词（今日/昨日等）
        if _ANSWER_REL_DATE_RE.search(answer):
            found_date = True
            found_date_relative = True
            if current_date:
                parsed = _parse_rel_date_in_text(answer, current_date)
                if parsed and parsed != canon:
                    logger.info("回答相对日期与 reference_date 不一致: 解析得 {} vs {}", parsed, canon)
                    return False

        # 2b. 回答含正面赛果（战胜/比分等）却仅有相对日期词、无显式日历日期时，不采信「今天/刚刚」，判不通过。
        #    例外：若回答在描述「进行中/目前比分」等当前态，则视为与「今天」一致，不触发 2b。
        if (
            reference_date
            and _POSITIVE_EVENT_CLAIM_RE.search(answer)
            and found_date_relative
            and not found_date_explicit
            and not _LIVE_OR_CURRENT_SCORE_RE.search(answer)
        ):
            logger.info("回答含正面赛果但仅用相对日期词无显式日历日期，无法与 reference_date 对齐，视为不通过")
            return False

        # 3. 语义策略：否定/当前态且 reference_date 为今天 -> 视为与「当前」一致，通过（且非正面赛果描述）
        if not found_date and current_date and not _POSITIVE_EVENT_CLAIM_RE.search(answer):
            try:
                today_str = datetime.strptime(current_date, "%Y-%m-%d").strftime("%Y-%m-%d")
                if canon == today_str and _CURRENT_OR_NEGATIVE_RE.search(answer):
                    logger.info("回答为否定/当前态表述且 reference_date 为今天，视为时间对齐通过")
                    return True
            except ValueError:
                pass

        if not found_date:
            logger.info("回答中无日期表述且非当前态/否定表述，无法与 reference_date 对齐，视为不通过")
            return False
        return True
