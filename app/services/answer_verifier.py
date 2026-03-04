# app/services/answer_verifier.py
"""
回答校验模块：生成与校验分离，单一职责。

职责边界：
- 捏造检测：回答中的事实是否均能在检索结果中找到依据。
- 扣题检测：回答是否针对用户问题（含「否定回答」语义：如「有X吗」->「没有X，目前有Y」视为扣题）。
- 时间对齐：回答中的时间表述是否与 reference_date 一致；对「否定/当前态」回答采用语义策略，不强制显式日期。
- 日期与 context 一致性：回答中的显式日期必须出现在检索素材中，不得捏造。

校验结果带失败原因，便于 Pipeline 做替换决策与 Tracer 可观测。
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
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


_WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_WEEK_REL_RE = re.compile(r'(上|本)周([一二三四五六日天])')


def _parse_rel_date_in_text(text: str, current_date: str) -> Optional[str]:
    """从文本解析「昨日」「今天」「上周六」等为 YYYY-MM-DD。"""
    if not text or not current_date:
        return None
    try:
        base = datetime.strptime(current_date, "%Y-%m-%d")
    except ValueError:
        return None
    from datetime import timedelta
    if "今日" in text or "今天" in text:
        return current_date
    if "昨日" in text or "昨天" in text:
        return (base - timedelta(days=1)).strftime("%Y-%m-%d")
    if "前天" in text or "前日" in text:
        return (base - timedelta(days=2)).strftime("%Y-%m-%d")
    m = _WEEK_REL_RE.search(text)
    if m:
        prefix, day_char = m.group(1), m.group(2)
        target_wd = _WEEKDAY_MAP[day_char]
        cur_wd = base.weekday()
        if prefix == "上":
            start_of_week = base - timedelta(days=cur_wd)
            result = start_of_week - timedelta(days=7) + timedelta(days=target_wd)
        else:
            start_of_week = base - timedelta(days=cur_wd)
            result = start_of_week + timedelta(days=target_wd)
        return result.strftime("%Y-%m-%d")
    return None


# 回答中显式日期
_ANSWER_DATE_RE = re.compile(
    r'(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日|(\d{4})-(\d{1,2})-(\d{1,2})'
)
# 从 context 正文提取日期（虎扑02月25日讯、2月24日等）
_CONTEXT_DATE_RE = re.compile(
    r'(?:(\d{4})年?)?(\d{1,2})月(\d{1,2})日|(\d{4})-(\d{2})-(\d{2})'
)
# 相对日期词（含上周X/本周X等可精确解析的表达）
_ANSWER_REL_DATE_RE = re.compile(r'昨日|昨天|前天|前日|今天|今日|[上本]周[一二三四五六日天]')
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


def _extract_dates_yymmdd(text: str, default_year: Optional[int] = None) -> set[str]:
    """
    从文本提取日期并规范为 YYYY-MM-DD。
    用于回答与 context 的日期一致性校验。
    """
    out: set[str] = set()
    year = default_year or datetime.now().year
    for m in _CONTEXT_DATE_RE.finditer(text or ""):
        g = m.groups()
        try:
            if g[1] is not None and g[2] is not None:
                y = int(g[0]) if g[0] else year
                mo, d = int(g[1]), int(g[2])
            elif g[3] is not None and g[4] is not None and g[5] is not None:
                y, mo, d = int(g[3]), int(g[4]), int(g[5])
            else:
                continue
            dt = datetime(y, mo, d)
            out.add(dt.strftime("%Y-%m-%d"))
        except ValueError:
            continue
    return out


def _extract_answer_dates(answer: str, default_year: Optional[int] = None) -> set[str]:
    """从回答中提取显式日期，规范为 YYYY-MM-DD。"""
    out: set[str] = set()
    year = default_year or datetime.now().year
    for m in _ANSWER_DATE_RE.finditer(answer or ""):
        g = m.groups()
        try:
            if g[1] is not None and g[2] is not None:
                y = int(g[0]) if g[0] else year
                mo, d = int(g[1]), int(g[2])
            elif g[3] is not None and g[4] is not None and g[5] is not None:
                y, mo, d = int(g[3]), int(g[4]), int(g[5])
            else:
                continue
            dt = datetime(y, mo, d)
            out.add(dt.strftime("%Y-%m-%d"))
        except ValueError:
            continue
    return out


def _extract_context_dates(context: List[Dict], current_year: Optional[int] = None) -> set[str]:
    """
    从检索 context 提取所有日期：published_time + 正文中的 X月X日。
    报道可能在次日发布当日事件，故相邻日也视为有效。
    """
    out: set[str] = set()
    year = current_year or datetime.now().year
    for item in context or []:
        pt = item.get("published_time")
        if pt:
            try:
                if "T" in str(pt):
                    dt = datetime.fromisoformat(str(pt).replace("Z", "+00:00"))
                else:
                    dt = datetime.strptime(str(pt)[:10], "%Y-%m-%d")
                out.add(dt.strftime("%Y-%m-%d"))
                out.add((dt - timedelta(days=1)).strftime("%Y-%m-%d"))
            except (ValueError, TypeError):
                pass
        raw = (item.get("content") or "") + (item.get("title") or "")
        out |= _extract_dates_yymmdd(raw, year)
    return out


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
        answer_scope_mode: str = "strict_date",
    ) -> VerificationResult:
        """
        并行执行捏造+扣题两项 LLM 校验，再串行做规则式时间对齐。
        reference_date 为回答范围日期（answer_scope_date），由 pipeline 从 temporal_scope 推导传入；有值时才做时间对齐。
        answer_scope_mode 为目标日约束策略（strict_date / report_day_ok）。
        """
        if not answer or not answer.strip():
            return VerificationResult.fail(VerifyFailureReason.OFF_TOPIC)

        # 捏造 + 扣题无数据依赖，并行调用 GLM API
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as pool:
            fab_future = pool.submit(
                self._verify_no_fabrication, context, answer, answer_scope_mode,
            )
            topic_future = pool.submit(
                self._verify_on_topic, query, answer,
            )
            fabrication_ok = fab_future.result()
            on_topic_ok = topic_future.result()
        parallel_ms = (time.perf_counter() - t0) * 1000
        fabrication_ms = parallel_ms
        on_topic_ms = parallel_ms

        logger.info(
            "事实核查(并行): 捏造={}, 答非所问={} | 耗时 {:.1f}ms",
            "通过" if fabrication_ok else "不通过",
            "通过" if on_topic_ok else "不通过",
            parallel_ms,
        )

        if not fabrication_ok:
            logger.info("事实核查不通过：捏造")
            return VerificationResult.fail(VerifyFailureReason.FABRICATION, fabrication_ms=fabrication_ms)
        if not on_topic_ok:
            logger.info("事实核查不通过：答非所问")
            return VerificationResult.fail(
                VerifyFailureReason.OFF_TOPIC,
                fabrication_ms=fabrication_ms,
                on_topic_ms=on_topic_ms,
            )

        # 时间对齐（规则式，无 LLM 调用，保持串行）
        t0 = time.perf_counter()
        time_ok = self._verify_temporal_alignment(answer, reference_date, current_date, answer_scope_mode=answer_scope_mode)
        if time_ok and context:
            ctx_date_ok = self._verify_context_date_consistency(answer, context, current_date)
            if not ctx_date_ok:
                time_ok = False
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

        total_ms = parallel_ms + temporal_ms
        logger.info("事实核查: 捏造=通过, 答非所问=通过, 时间对齐=通过 | 总耗时 {:.1f}ms", total_ms)
        return VerificationResult.ok(
            fabrication_ms=fabrication_ms,
            on_topic_ms=on_topic_ms,
            temporal_ms=temporal_ms,
        )

    def _verify_no_fabrication(self, context: List[Dict], answer: str, answer_scope_mode: str = "strict_date") -> bool:
        """捏造检测：回答中的事实是否均能在检索结果中找到依据。来源为赛况数据引擎的条目给予更长截断，主要作格式/一致性校验。"""
        lines = []
        for i, item in enumerate(context, 1):
            src = item.get("source", "")
            title = item.get("title", "")
            raw = item.get("content") or ""
            if src == "赛况数据引擎":
                cap = 2000
            elif src == "对话历史":
                cap = 1500
            else:
                cap = 800
            content = raw[:cap] if cap else raw
            if len(raw) > cap:
                content = content + "…（已截断）"
            lines.append(f"{i}. 来源：{src} 标题：{title} 内容摘要：{content}")
        context_block = "\n".join(lines) if lines else "（无）"

        date_rule = (
            "- 不通过：**日期必须来自素材**。回答中的具体日期（如 X月X日、YYYY年MM月DD日）若未在检索结果正文、标题或报道时间（published_time）中出现，则判不通过。\n"
            "- 不通过（事件-日期绑定错误）：回答中事件描述与日期的绑定必须与素材一致。"
            "例如素材写「27日全部出炉」「当天的比赛中晋级」的事件，回答中必须归属到27日，不可归入其他日期。"
            "素材中某事件绑定的日期为X日，回答却将其放在Y日的播报下，视为捏造。"
        )
        if answer_scope_mode == "report_day_ok":
            date_rule = (
                "- 不通过：回答中出现了素材中完全不存在的日期。但**日期格式补全不算捏造**：素材写「27日」而回答写「2026年2月27日」属于合理推断，应判通过；同理「X日」→「YYYY年M月X日」均通过。\n"
                "- 通过：回答开头声明「未检索到某日当天的比赛」并随后播报前几日事件，这是正常指令行为，不算捏造。"
            )

        user_content = f"""你是事实核查员，只做一件事：判断「回答」中的事实是否都能在「提供的参考内容」中找到依据。

规则：
- 通过：回答中的事件、数据、来源名称、日期等均能在参考内容中找到对应或合理归纳。
- 不通过：回答中出现了参考内容里完全不存在的事件、具体数据或来源名称（凭空编造）。
{date_rule}
- 不通过：若参考内容中某场比赛标注为「进行中」，但回答中对该场使用了「获胜」「击败」「取胜」「险胜」等表示已结束的措辞，则判不通过（将进行中当作已结束即属捏造）。

重要：本步骤只判断有无捏造，不判断是否扣题。即使用户问的是A而回答讲的是B，只要回答里的每一条事实（事件、数据、来源）都能在下方参考内容中找到，仍应判「通过」；仅当回答中出现参考内容中不存在的事件/数据/来源时判「不通过」。

参考内容：
{context_block}

回答：
{answer}

只输出「通过」或「不通过」。"""
        out = (self._llm.chat([{"role": "user", "content": user_content}], temperature=0.1, max_tokens=50)) or ""
        out = out.strip().replace(" ", "")
        passed = "不通过" not in out
        return passed

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
        out = (self._llm.chat([{"role": "user", "content": user_content}], temperature=0.1, max_tokens=50)) or ""
        out = out.strip().replace(" ", "")
        passed = "不通过" not in out
        return passed

    def _verify_temporal_alignment(
        self,
        answer: str,
        reference_date: Optional[str],
        current_date: Optional[str],
        answer_scope_mode: str = "strict_date",
    ) -> bool:
        """
        时间对齐：回答中的时间表述与 reference_date 一致。
        策略：
        - 无 reference_date -> 通过。
        - report_day_ok: 回答明确说明未找到目标日事件 + 其他日期来自 context -> 通过。
        - strict_date: 回答含显式日期或「昨日/今天」等 -> 与 reference_date 比较，不一致则不过。
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

        # report_day_ok 独立分支：允许回答中包含非目标日的事件日期，
        # 日期准确性由下游 _verify_context_date_consistency 兜底（确保来自素材）。
        if answer_scope_mode == "report_day_ok":
            logger.info("report_day_ok 模式: 跳过严格日期对齐, 由 context 日期一致性检查兜底")
            return True

        has_target_date = False
        found_date_explicit = False
        found_date_relative = False

        # 1. 显式日期：检查目标日期是否出现；非目标日期不再直接拒绝，
        #    其准确性由 _verify_context_date_consistency + _verify_no_fabrication 兜底。
        for m in _ANSWER_DATE_RE.finditer(answer):
            found_date_explicit = True
            g = m.groups()
            if g[1] is not None and g[2] is not None:
                y = int(g[0]) if g[0] else ref_dt.year
                mo, d = int(g[1]), int(g[2])
            elif g[3] is not None and g[4] is not None and g[5] is not None:
                y, mo, d = int(g[3]), int(g[4]), int(g[5])
            else:
                continue
            try:
                ans_dt = datetime(y, mo, d)
                if ans_dt.date() == ref_dt.date():
                    has_target_date = True
            except ValueError:
                continue

        # 2. 相对日期词（今日/昨日/上周六等）—— 仅在无显式日期时做严格校验
        rel_date_resolved = False
        if _ANSWER_REL_DATE_RE.search(answer):
            found_date_relative = True
            if current_date:
                parsed = _parse_rel_date_in_text(answer, current_date)
                if parsed and parsed == canon:
                    has_target_date = True
                    rel_date_resolved = True
                elif parsed and parsed != canon and not found_date_explicit:
                    logger.info("回答相对日期与 reference_date 不一致: 解析得 {} vs {}", parsed, canon)
                    return False

        # 2b. 回答含正面赛果却仅有模糊相对日期词（今天/刚刚等）、无显式日历日期时，判不通过。
        #     「上周六」等可精确解析且已匹配 reference_date 的表达不受此限制。
        if (
            reference_date
            and _POSITIVE_EVENT_CLAIM_RE.search(answer)
            and found_date_relative
            and not found_date_explicit
            and not rel_date_resolved
            and not _LIVE_OR_CURRENT_SCORE_RE.search(answer)
        ):
            logger.info("回答含正面赛果但仅用模糊相对日期词无显式日历日期，无法与 reference_date 对齐，视为不通过")
            return False

        # 3. 语义策略：否定/当前态且 reference_date 为今天 -> 视为与「当前」一致，通过
        found_date = found_date_explicit or found_date_relative
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

        if not has_target_date:
            logger.info("回答中未出现目标日期 {}，视为不通过", canon)
            return False

        return True

    def _verify_context_date_consistency(
        self,
        answer: str,
        context: List[Dict],
        current_date: Optional[str],
    ) -> bool:
        """
        回答日期与 context 日期一致性：回答中的显式日期必须出现在检索素材中。
        规则校验，不调 LLM。无显式日期时通过。
        """
        answer_dates = _extract_answer_dates(answer)
        if not answer_dates:
            return True
        try:
            year = int(current_date[:4]) if current_date and len(current_date) >= 4 else None
        except (ValueError, TypeError):
            year = None
        context_dates = _extract_context_dates(context, year)
        for ad in answer_dates:
            if ad not in context_dates:
                logger.info("回答日期与 context 不一致: 回答中 {} 未出现在检索素材中", ad)
                return False
        return True
