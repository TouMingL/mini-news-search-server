# app/services/intent_classifier.py
"""
分类层 - Intent Classifier（规则前置 + 两阶段设计）

Layer 0 - 规则前置（零延迟）:
    对明确不需要搜索的查询（闲聊/知识问答/主观评价/乱码）直接拦截，不走 LLM。
    对明确需要搜索的查询（含时效性关键词 + 领域关键词）直接放行。

Layer 1 - needs_search 二分类（LLM，极简 prompt，高准确率）:
    仅判断"需不需要检索新闻/行情"，单一任务。

Layer 2 - 细分类（LLM，仅 needs_search=true 时执行）:
    对需要检索的查询做 filter_category / time_sensitivity / reference_datetime 分类。
"""
import json
import re
from datetime import datetime
from typing import List, Optional
from loguru import logger

from app.services.schemas import ClassificationResult, HistoryMessage


# ---------------------------------------------------------------------------
# 规则常量
# ---------------------------------------------------------------------------

# 闲聊 / 问候 / 无意义 → needs_search=False, intent_type=chitchat
_CHITCHAT_EXACT = frozenset([
    '你好', '您好', '嗨', 'hi', 'hello', '早', '早安', '晚安', '早上好', '下午好', '晚上好',
    '再见', '拜拜', 'bye', '好的', '好', 'ok', '嗯', '嗯嗯', '行', '可以', '收到',
    '明白', '了解', '知道了', '懂了', '谢谢', '感谢', '多谢', 'thanks', '谢了',
    '不', '不了', '不用', '算了', '没事', '哈哈', '呵呵', '哦', '啊', '嗯哼', '666', '牛', '厉害',
    '你是谁', '你叫什么', '你是什么',
])

# 知识 / 百科 / 主观评价 / 计算 → needs_search=False, intent_type=knowledge
_KNOWLEDGE_PATTERNS = [
    '什么是', '是什么', '怎么理解', '原理', '定义', '区别是',
    '怎么做', '怎么用', '如何', '为什么', '能不能', '可以吗',
    '帮我算', '计算', '等于多少',
    '好不好', '牛不牛', '厉不厉害', '值不值', '推荐吗', '怎么样',
    '是真的吗', '对不对', '有什么用',
]
# 弱放行阻断：含这些词视为明确知识问句，不触发「有时效词即放行」。不含「怎么样」「如何」，以便「XX最近打得怎么样」可弱放行
_KNOWLEDGE_BLOCK_FOR_WEAK_PASS = (
    '什么是', '是什么', '怎么理解', '原理', '定义', '区别是',
    '为什么', '怎么做', '怎么用', '能不能', '可以吗',
    '帮我算', '计算', '等于多少',
    '好不好', '牛不牛', '厉不厉害', '值不值', '推荐吗',
    '是真的吗', '对不对', '有什么用',
)

# 体育检索且用户问法涉及比分/赛果/表现时，执行层将附带赛况数据引擎；分类层基于原始 query 判定
# 含「打得/打的」两种常见写法，避免「打的如何」漏挂赛况数据引擎
_SCORE_SEEKING_KEYWORDS = (
    "比分", "得分", "赛果", "战况", "几比几", "胜负", "赢了", "输了", "领先", "落后",
    "今日赛况", "昨日赛果", "比赛结果", "最新赛果",
    "打得怎么样", "打得如何", "打得怎样", "打的怎么样", "打的如何", "打的怎样",
    "结果如何", "赢了吗", "输了吗", "多少分", "情况如何", "比赛情况", "近况", "赛况",
)

# 用户明确只要新闻或明确不要赛况/比分时，不得挂赛况数据引擎（优先于 _SCORE_SEEKING_KEYWORDS）
_SCORE_REJECTING_PATTERNS = (
    "不要赛况", "不要比分", "不要赛果", "不用赛况", "无需赛况", "仅要新闻", "只要新闻",
    "只要资讯", "仅新闻", "新闻就行", "新闻即可", "有没有新闻", "有什么新闻", "要新闻",
)


def _query_rejects_scores(lower: str) -> bool:
    """用户是否明确只要新闻或明确不要赛况/比分；是则不应 need_scores。"""
    return bool(lower and any(pat in lower for pat in _SCORE_REJECTING_PATTERNS))


def _query_seeks_scores(lower: str) -> bool:
    """原始 query 是否涉及比分/赛果/表现（用于设置 need_scores）。用户明确排除赛况时返回 False。"""
    if not lower or _query_rejects_scores(lower):
        return False
    return any(kw in lower for kw in _SCORE_SEEKING_KEYWORDS)

# 乱码检测：非中日韩字符 + 非常见英文字母组合
_GIBBERISH_RE = re.compile(
    r'^[a-zA-Z0-9\s\W]{3,}$'  # 纯 ASCII 且无明显英文单词
)
_COMMON_ENGLISH = re.compile(
    r'\b(?:the|is|are|what|how|why|who|when|where|can|do|news|price|stock)\b',
    re.IGNORECASE,
)


def _is_scores_tool_query(lower: str) -> bool:
    """含「比分」且与 NBA/比赛 相关时走赛况数据引擎，不检索，从根源上杜绝幻觉。"""
    if "比分" not in lower:
        return False
    return any(
        kw in lower for kw in ("nba", "比赛", "今日", "昨天", "战况", "赛果", "篮球")
    )


def _is_short_mixed_or_meaningless(query: str) -> bool:
    """
    短句且混合 CJK/ASCII 或无任何领域/时效信号，视为无意义，避免高置信度走检索。
    例如：「分为fwe」「a啊b」等。
    """
    stripped = query.strip()
    if len(stripped) > 6:
        return False
    lower = stripped.lower()
    has_timeliness = any(kw in lower for kw in _TIMELINESS_KEYWORDS)
    matched_cat = _match_category(lower)
    if has_timeliness or matched_cat:
        return False
    has_cjk = bool(re.search(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', stripped))
    has_non_cjk = bool(re.search(r'[a-zA-Z0-9]', stripped))
    if has_cjk and has_non_cjk:
        return True
    if len(stripped) <= 3 and re.match(r'^[a-zA-Z0-9\s\W]+$', stripped):
        return True
    return False

# 时效性强信号词 → 几乎一定需要搜索
_TIMELINESS_KEYWORDS = (
    '新闻', '快讯', '最新', '最近', '今天', '今日', '昨天', '本周',
    '近期', '近况', '刚刚', '突发', '实时', '行情', '走势', '涨了', '跌了',
    '多少钱', '现在价格', '当前',
)

# 领域关键词 → filter_category 映射（规则层 + LLM 层共用）
_CATEGORY_KEYWORDS = {
    "economy": [
        '黄金', '白银', '金价', '银价', '原油', '石油', '天然气', '油价',
        '股票', '股市', '大盘', 'a股', '上证', '深证', '创业板',
        '汇率', '美元', '欧元', '人民币', '日元', '英镑',
        '大豆', '玉米', '小麦', '棉花', '期货',
        '宏观', '财经', '金融', '基金', '债券', 'gdp', 'cpi',
    ],
    "tech": ['ai', '芯片', '互联网', '手机', '数码', '科技', '大模型', '机器人', '半导体'],
    "sports": [
        '足球', '篮球', '奥运', '羽毛球', '网球', '乒乓球',
        '体育', '赛事', '比赛', 'nba', 'cba', '火箭', '湖人', '勇士', '杜兰特', '詹姆斯',
        '马刺', '凯尔特人', '热火', '独行侠', '太阳', '掘金', '快船', '雄鹿', '76人', '尼克斯',
        '世界杯', '欧冠', '英超', '转会', '比分', '联赛',
    ],
    "world": ['中美', '俄乌', '地缘', '国际', '政治', '外交', '制裁', '关税'],
    "health": ['健康', '医药', '养生', '新药', '疫苗', '疫情'],
    "academic": ['学术', '论文', '研究', '期刊'],
}

# legacy category 映射（LLM 可能输出的非标准值）
_LEGACY_CATEGORY_MAP = {
    "贵金属": "economy", "能源": "economy", "股指": "economy", "外汇": "economy",
    "农产品": "economy", "宏观": "economy", "科技": "tech", "政治": "world",
    "社会": "general", "体育": "sports", "天气": "general", "常识": "general",
    "其他": "general", "entertainment": "general", "military": "world",
    "education": "academic", "finance": "economy", "business": "economy",
}

_VALID_CATEGORIES = frozenset(["academic", "world", "tech", "economy", "sports", "health", "general"])

# ---------------------------------------------------------------------------
# needs_search 二分类 Prompt（Stage 1，极简）
# ---------------------------------------------------------------------------

_NEEDS_SEARCH_PROMPT = """判断以下查询是否需要检索最近的新闻、行情、政策等时效性信息。
只输出 true 或 false，不要任何解释。

判断标准：
- true：用户想了解最近发生的事、新闻动态、价格行情、政策变化、比赛结果等时效性信息
- false：闲聊、主观评价、常识百科、概念解释、历史知识、数学计算、个人建议、对前文已知信息的追问
{history_block}
查询：{query}

需要检索："""

# ---------------------------------------------------------------------------
# 细分类 Prompt（Stage 2，仅 needs_search=true 时执行）
# ---------------------------------------------------------------------------

_CATEGORY_OPTIONS = "academic | world | tech | economy | sports | health | general"

_DETAIL_CLASSIFY_PROMPT = """你是一个意图分类助手。对以下需要检索的查询进行分类，只输出JSON。

今日日期：{current_date}
查询：{query}

分类字段：
1. intent_type: news（新闻事件）| realtime_quote（实时行情价格）
2. filter_categories（检索类别 top3，按与查询相关度从高到低排列，最多 3 个）:
   可选值：{category_options}
   核心原则：按主体与查询相关度排序。如"NBA交易"→["sports"]，"苹果股价与芯片"→["tech","economy"]。只填 1～3 个。
3. time_sensitivity: realtime | recent | historical | none
4. confidence: 0-1
5. reference_datetime: 用户提及的日期(YYYY-MM-DD)或null

只输出一行紧凑JSON，filter_categories 为数组：
{{"intent_type":"news","filter_categories":["general"],"time_sensitivity":"recent","confidence":0.9,"reference_datetime":null}}"""


class IntentClassifier:
    """
    意图分类器（规则前置 + 两阶段 LLM）

    classify() 入口:
        Layer 0: 规则前置 → 能确定的直接返回
        Layer 1: needs_search 二分类 (LLM) → 极简 prompt
        Layer 2: 细分类 (LLM) → 仅 needs_search=true 时
    """

    # 暴露给 Pipeline tracer（兼容旧接口）；format 时需传入 category_options
    CLASSIFY_PROMPT = _DETAIL_CLASSIFY_PROMPT
    CATEGORY_OPTIONS = _CATEGORY_OPTIONS

    def __init__(self, local_llm_service=None):
        self._local_llm = local_llm_service

    @property
    def local_llm(self):
        if self._local_llm is None:
            from app.services.local_llm_service import get_local_llm_service
            self._local_llm = get_local_llm_service()
        return self._local_llm

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def classify(
        self,
        standalone_query: str,
        current_date: Optional[str] = None,
        history: Optional[List[HistoryMessage]] = None,
        original_query: Optional[str] = None,
        reference_date: Optional[str] = None,
    ) -> ClassificationResult:
        if current_date is None:
            current_date = datetime.now().strftime("%Y-%m-%d")

        query = standalone_query.strip()

        # ===== Layer 0: 规则前置 =====
        rule_result = self._rule_pre_filter(query)
        if rule_result is not None:
            logger.info(
                f"[Classifier] 规则前置命中: needs_search={rule_result.needs_search}, "
                f"intent={rule_result.intent_type}, reason=rule"
            )
            if reference_date and rule_result.reference_datetime is None:
                rule_result = rule_result.model_copy(update={"reference_datetime": reference_date})
            return rule_result

        # ===== Layer 1: needs_search 二分类 (LLM) =====
        needs_search = self._llm_needs_search(query, history=history, original_query=original_query)

        if not needs_search:
            logger.info(f"[Classifier] LLM 判定不需要检索: {query}")
            result = ClassificationResult(
                needs_search=False,
                need_retrieval=False,
                need_scores=False,
                intent_type="knowledge",
                filter_category="general",
                filter_categories=["general"],
                time_sensitivity="none",
                confidence=0.8,
            )
            if reference_date:
                result = result.model_copy(update={"reference_datetime": reference_date})
            return result

        # ===== Layer 2: 细分类 (LLM) =====
        logger.info(f"[Classifier] 需要检索，进入细分类: {query}")
        result = self._llm_detail_classify(query, current_date)
        if reference_date and result.reference_datetime is None:
            result = result.model_copy(update={"reference_datetime": reference_date})
        return result

    # ------------------------------------------------------------------
    # Layer 0: 规则前置
    # ------------------------------------------------------------------

    @staticmethod
    def _rule_pre_filter(query: str) -> Optional[ClassificationResult]:
        """
        纯规则判断。能确定时返回 ClassificationResult，不确定返回 None 交给 LLM。

        顺序原则：时效+领域、新闻/快讯 优先于 知识模式。否则「最近XX怎么样」会先命中
        「怎么样」被判为常识，导致第一步就歪。
        """
        lower = query.lower().strip()

        # 1. 精确匹配闲聊
        if lower in _CHITCHAT_EXACT:
            return ClassificationResult(
                needs_search=False, need_retrieval=False, need_scores=False,
                intent_type="chitchat",
                filter_category="general", filter_categories=["general"],
                time_sensitivity="none",                 confidence=0.95,
            )

        # 1.5 赛况数据引擎(scores_only)：NBA/比赛 比分 → 仅读 JSON，不检索、不生成
        if _is_scores_tool_query(lower):
            return ClassificationResult(
                needs_search=False,
                need_retrieval=False,
                need_scores=True,
                intent_type="tool_scores",
                filter_category="sports",
                filter_categories=["sports"],
                time_sensitivity="none",
                confidence=0.95,
            )

        # 2. 强时效性信号 + 领域关键词 → 直接放行搜索
        has_timeliness = any(kw in lower for kw in _TIMELINESS_KEYWORDS)
        matched_cat = _match_category(lower)
        if has_timeliness and matched_cat:
            time_sens = "realtime" if any(
                kw in lower for kw in ('今天', '今日', '实时', '当前', '现在', '多少钱', '现在价格')
            ) else "recent"
            return ClassificationResult(
                needs_search=True,
                need_retrieval=True,
                need_scores=(matched_cat == "sports" and _query_seeks_scores(lower)),
                intent_type="realtime_quote" if time_sens == "realtime" else "news",
                filter_category=matched_cat,
                filter_categories=[matched_cat],
                time_sensitivity=time_sens,
                confidence=0.9,
            )

        # 3. 含"新闻"/"快讯"等明确搜索意图词
        if any(kw in lower for kw in ('新闻', '快讯', '头条', '突发')):
            return ClassificationResult(
                needs_search=True,
                need_retrieval=True,
                need_scores=((matched_cat or "general") == "sports" and _query_seeks_scores(lower)),
                intent_type="news",
                filter_category=matched_cat or "general",
                filter_categories=[matched_cat] if matched_cat else ["general"],
                time_sensitivity="recent",
                confidence=0.9,
            )

        # 3.5 弱放行：有时效词 且 无明确知识问句
        if has_timeliness and not any(pat in lower for pat in _KNOWLEDGE_BLOCK_FOR_WEAK_PASS):
            time_sens = "realtime" if any(
                kw in lower for kw in ('今天', '今日', '实时', '当前', '现在', '多少钱', '现在价格')
            ) else "recent"
            return ClassificationResult(
                needs_search=True,
                need_retrieval=True,
                need_scores=((matched_cat or "general") == "sports" and _query_seeks_scores(lower)),
                intent_type="realtime_quote" if time_sens == "realtime" else "news",
                filter_category=matched_cat or "general",
                filter_categories=[matched_cat] if matched_cat else ["general"],
                time_sensitivity=time_sens,
                confidence=0.85,
            )

        # 3.6 体育主体 + 表现/近况类问法（无时效词时）
        _SPORTS_PERFORMANCE_PATTERNS = (
            '打得怎么样', '打得如何', '打得怎样', '打的怎么样', '打的如何', '打的怎样',
            '近况', '赛况', '比赛结果',
        )
        if matched_cat == "sports" and any(pat in lower for pat in _SPORTS_PERFORMANCE_PATTERNS):
            return ClassificationResult(
                needs_search=True,
                need_retrieval=True,
                need_scores=_query_seeks_scores(lower),
                intent_type="news",
                filter_category="sports",
                filter_categories=["sports"],
                time_sensitivity="recent",
                confidence=0.9,
            )

        # 4. 知识 / 主观评价模式（在时效/检索之后，避免「最近XX怎么样」被误判）
        for pat in _KNOWLEDGE_PATTERNS:
            if pat in lower:
                return ClassificationResult(
                    needs_search=False, need_retrieval=False, need_scores=False,
                    intent_type="knowledge",
                    filter_category="general", filter_categories=["general"],
                    time_sensitivity="none", confidence=0.85,
                )

        # 5. 疑似乱码（纯 ASCII 且不含常见英文词）
        if _GIBBERISH_RE.match(query) and not _COMMON_ENGLISH.search(query):
            return ClassificationResult(
                needs_search=False, need_retrieval=False, need_scores=False,
                intent_type="chitchat",
                filter_category="general", filter_categories=["general"],
                time_sensitivity="none", confidence=0.9,
            )

        # 5.5 短句混合/无意义（如「分为fwe」）：无领域信号则不走检索
        if _is_short_mixed_or_meaningless(query):
            return ClassificationResult(
                needs_search=False, need_retrieval=False, need_scores=False,
                intent_type="chitchat",
                filter_category="general", filter_categories=["general"],
                time_sensitivity="none", confidence=0.0,
            )

        # 不确定 → 交给 LLM
        return None

    # ------------------------------------------------------------------
    # Layer 1: needs_search 二分类 (LLM)
    # ------------------------------------------------------------------

    @staticmethod
    def _format_history_for_search_check(
        history: Optional[List[HistoryMessage]],
    ) -> str:
        """
        为 needs_search 判断构造历史摘要。
        仅取最近 1 轮（1 user + 1 assistant），assistant 截断至 100 字。
        """
        if not history or len(history) < 2:
            return ""
        recent = history[-2:]  # 最近 1 轮
        lines = []
        for msg in recent:
            content = msg.content.strip()
            if len(content) > 100:
                content = content[:100] + "..."
            role = "用户" if msg.role == "user" else "助手"
            lines.append(f"{role}：{content}")
        return "\n最近对话摘要：\n" + "\n".join(lines) + "\n"

    def _llm_needs_search(
        self,
        query: str,
        history: Optional[List[HistoryMessage]] = None,
        original_query: Optional[str] = None,
    ) -> bool:
        """用极简 prompt 让 LLM 做 true/false 二分类，注入对话历史作为上下文。"""
        if not self.local_llm.is_available:
            logger.warning("[Classifier] 本地模型不可用，默认不搜索")
            return False

        history_block = self._format_history_for_search_check(history)
        # 当原始表述与改写后差异较大时，同时展示两者，
        # 让分类器看到原始语气/情感信号（改写过程中容易丢失）
        if original_query and original_query.strip() != query.strip():
            display_query = f"{query}\n（用户原始表述：{original_query}）"
        else:
            display_query = query
        prompt = _NEEDS_SEARCH_PROMPT.format(
            query=display_query,
            history_block=history_block,
        )
        try:
            raw = self.local_llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=8,
            ).strip().lower()
            result = raw.startswith("true") or raw == "是" or raw == "yes"
            logger.debug(f"[Classifier] needs_search LLM raw='{raw}' -> {result}")
            return result
        except Exception as e:
            logger.error(f"[Classifier] needs_search LLM 调用失败: {e}")
            return False

    # ------------------------------------------------------------------
    # Layer 2: 细分类 (LLM)
    # ------------------------------------------------------------------

    def _llm_detail_classify(self, query: str, current_date: str) -> ClassificationResult:
        """对需要检索的查询做 category/time_sensitivity 细分类。"""
        prompt = _DETAIL_CLASSIFY_PROMPT.format(
            current_date=current_date,
            query=query,
            category_options=_CATEGORY_OPTIONS,
        )
        messages = [{"role": "user", "content": prompt}]

        # 尝试 Schema 约束
        if self.local_llm.is_available:
            try:
                result = self.local_llm.chat_with_schema(
                    messages=messages,
                    response_schema=_DetailClassifyResult,
                    temperature=0.1,
                    max_tokens=256,
                )
                return _to_classification(result, query)
            except Exception as e:
                logger.warning(f"[Classifier] Schema 细分类失败，降级 JSON 解析: {e}")

            # 降级：JSON 解析
            try:
                raw = self.local_llm.chat(
                    messages=messages,
                    temperature=0.1,
                    max_tokens=256,
                )
                return _parse_detail_json(raw, query)
            except Exception as e2:
                logger.error(f"[Classifier] JSON 解析也失败: {e2}")

        # 最终降级：规则
        logger.warning("[Classifier] LLM 全部失败，使用规则降级")
        return _rule_fallback_classify(query)


# ---------------------------------------------------------------------------
# Stage 2 Pydantic schema（仅 needs_search=true 时的字段，比完整版简单）
# ---------------------------------------------------------------------------

from pydantic import BaseModel, Field  # noqa: E402
from typing import List, Literal  # noqa: E402


class _DetailClassifyResult(BaseModel):
    intent_type: Literal["news", "realtime_quote"] = "news"
    filter_categories: List[str] = Field(
        default_factory=lambda: ["general"],
        description="检索类别 top3，按相关度排序，最多 3 个",
    )
    time_sensitivity: Literal["realtime", "recent", "historical", "none"] = "recent"
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    reference_datetime: Optional[str] = None


def _normalize_categories(cats: List[str], query: str) -> List[str]:
    """规范为合法类别列表，最多 3 个；非法项用 general 或规则匹配替代。"""
    out: List[str] = []
    for c in (cats or [])[:3]:
        c = _LEGACY_CATEGORY_MAP.get(c, c)
        if c in _VALID_CATEGORIES:
            out.append(c)
        else:
            out.append(_match_category(query.lower()) or "general")
    if not out:
        out = [_match_category(query.lower()) or "general"]
    return out[:3]


def _to_classification(detail: _DetailClassifyResult, query: str = "") -> ClassificationResult:
    """将 Stage 2 结果转为完整 ClassificationResult（needs_search=True）。"""
    cats = _normalize_categories(detail.filter_categories, query)
    primary = cats[0] if cats else "general"
    lower = (query or "").strip().lower()
    return ClassificationResult(
        needs_search=True,
        need_retrieval=True,
        need_scores=("sports" in cats and _query_seeks_scores(lower)),
        intent_type=detail.intent_type,
        filter_category=primary,
        filter_categories=cats,
        time_sensitivity=detail.time_sensitivity,
        confidence=detail.confidence,
        reference_datetime=detail.reference_datetime,
    )


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _match_category(lower_query: str) -> Optional[str]:
    """用关键词匹配 filter_category，匹配不到返回 None。"""
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in lower_query:
                return cat
    return None


def _parse_detail_json(raw_output: str, query: str) -> ClassificationResult:
    """解析 LLM 输出的 JSON 并容错。"""
    text = raw_output.strip()

    # 去 markdown 代码块
    if "```" in text:
        start = text.find("```")
        rest = text[start + 3:]
        if rest.startswith("json"):
            rest = rest[4:].lstrip()
        end = rest.find("```")
        text = rest[:end].strip() if end >= 0 else rest.strip()

    # 提取 JSON
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}") + 1
        if s >= 0 and e > s:
            data = json.loads(text[s:e])
        else:
            raise ValueError(f"无法解析JSON: {text}")

    intent_type = data.get("intent_type", "news")
    if intent_type not in ("news", "realtime_quote"):
        intent_type = "news"

    raw_cats = data.get("filter_categories") or data.get("filter_category")
    if isinstance(raw_cats, str):
        raw_cats = [raw_cats]
    if not isinstance(raw_cats, list):
        raw_cats = ["general"]
    filter_categories = _normalize_categories(raw_cats[:3], query)
    filter_category = filter_categories[0] if filter_categories else "general"

    time_sensitivity = data.get("time_sensitivity", "recent")
    if time_sensitivity not in ("realtime", "recent", "historical", "none"):
        time_sensitivity = "recent"

    confidence = data.get("confidence", 0.8)
    if not isinstance(confidence, (int, float)):
        confidence = 0.8
    confidence = max(0.0, min(1.0, float(confidence)))

    reference_datetime = data.get("reference_datetime")
    if reference_datetime is not None:
        reference_datetime = str(reference_datetime).strip()
        if reference_datetime.lower() in ("null", "none", ""):
            reference_datetime = None
        else:
            try:
                datetime.strptime(reference_datetime, "%Y-%m-%d")
            except ValueError:
                logger.warning(f"reference_datetime 格式异常: {reference_datetime}")
                reference_datetime = None

    return ClassificationResult(
        needs_search=True,
        need_retrieval=True,
        need_scores=("sports" in filter_categories and _query_seeks_scores((query or "").lower())),
        intent_type=intent_type,
        filter_category=filter_category,
        filter_categories=filter_categories,
        time_sensitivity=time_sensitivity,
        confidence=confidence,
        reference_datetime=reference_datetime,
    )


def _rule_fallback_classify(query: str) -> ClassificationResult:
    """LLM 全部失败时的规则降级分类。"""
    lower = query.lower()
    cat = _match_category(lower) or "general"
    time_sens = "realtime" if any(
        kw in lower for kw in ('今天', '今日', '实时', '当前', '现在')
    ) else "recent"
    return ClassificationResult(
        needs_search=True,
        need_retrieval=True,
        need_scores=(cat == "sports" and _query_seeks_scores(lower)),
        intent_type="realtime_quote" if time_sens == "realtime" else "news",
        filter_category=cat,
        filter_categories=[cat],
        time_sensitivity=time_sens,
        confidence=0.5,
    )


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

_intent_classifier_instance: Optional[IntentClassifier] = None


def get_intent_classifier() -> IntentClassifier:
    """获取 IntentClassifier 单例"""
    global _intent_classifier_instance
    if _intent_classifier_instance is None:
        _intent_classifier_instance = IntentClassifier()
    return _intent_classifier_instance
