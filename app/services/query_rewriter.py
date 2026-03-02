# app/services/query_rewriter.py
"""
预处理层 - Query Rewriter（两阶段设计）

Stage 1 - Independence Check（保守策略，反转默认行为）:
    只有能确认独立时才跳过改写，其余一律送 LLM。
    解决中文零代词/隐式指代的天然缺陷。

Stage 2 - Rewrite（调用本地 LLM）:
    使用最近 2 轮历史（仅用户侧）改写为独立查询。
"""
from typing import List, Optional
from loguru import logger

from app.services.schemas import HistoryMessage, RewriteResult


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 独立短表达（问候/告别/确认/感谢/情绪等），永远不需要改写
_STANDALONE_EXPRESSIONS = frozenset([
    # 问候
    '你好', '您好', '嗨', 'hi', 'hello', '早', '早安', '晚安', '早上好', '下午好', '晚上好',
    # 告别
    '再见', '拜拜', 'bye', '拜',
    # 确认/应答
    '好的', '好', 'ok', '嗯', '嗯嗯', '行', '可以', '收到', '明白', '了解', '知道了', '懂了',
    # 感谢
    '谢谢', '感谢', '多谢', 'thanks', '谢了',
    # 否定
    '不', '不了', '不用', '算了', '没事',
    # 情绪/语气
    '哈哈', '呵呵', '哦', '啊', '嗯哼', '666', '牛', '厉害',
])

# 强依赖信号词 —— 出现即判定为 context-dependent（快速路径，省掉 LLM 调用）
_DEPENDENCY_SIGNALS = (
    # 显式指代词
    '它', '这个', '那个', '这', '那', '他们', '她们', '它们',
    # 省略表达
    '继续', '还有', '然后呢', '接着', '再说说', '详细', '更多', '还有呢',
    # 精炼 / 排除意图
    '不要', '不看', '换一个', '换一批', '其他的', '排除', '别推', '换成', '除了',
    # 副词关联（承接上文的"也/又/还"）
    '也是', '也有', '也能', '也会', '也死', '也去', '也在', '也要',
    '又是', '又有', '又怎',
    '还是', '还能', '还会', '还在',
)

# 弱依赖信号词 —— 短查询中出现时暗示上下文依赖，用于 _has_novel_named_entity 判断
_WEAK_DEPENDENCY_HINTS = ('也', '又', '还', '呢', '吗')

# 改写时使用的最大历史轮数（1 轮 = 1 user + 1 assistant）
_REWRITE_MAX_TURNS = 2

# 上轮主题在 prompt 中的最大展示长度（避免超限，仅作锚定用）
_LAST_SUBJECT_MAX_CHARS = 80

# 改写 Prompt —— 规则 1 就是"新话题直接原样返回"
_REWRITE_PROMPT = """你是一个查询改写助手。将用户当前输入改写为一个独立、无需上下文即可理解的查询。
{category_constraint}
规则（按优先级排列）：
1. 若当前输入引入了全新话题（历史中未出现的实体/领域），直接原样返回，不要用历史覆盖
2. 消解指代词（它/这个/那个 -> 具体实体名称）
3. 补全省略信息（"继续" -> "继续[具体内容]"）
4. 精炼/排除意图：用户说「不要XX」「换一个」时，结合最近一轮主题改写。例如上一轮问「体育新闻」，当前说「不要足球」-> 「体育新闻 排除足球」
5. **时间切换**：当用户当前输入仅为时间切换（如「那昨天呢」「那今天呢」「昨天呢」）且下方给出了「上一轮独立查询或用户输入」时，必须继承上轮主体，输出「[时间] + [上轮主体]」。例如上轮为「勇士队比分」或「那勇士呢」、当前输入「那昨天呢」-> 输出「昨天 勇士队比分」或「勇士队 昨天 比分」，不得泛化为「昨天的篮球比赛怎么样」
6. 保持原意，不添加推测
7. 输出格式：第一行是改写后的独立查询；第二行若进行了实质性改写则写「改写原因：」加一行简短说明（如：消解指代、补全主题、继承上轮主体+时间切换），否则写「无」。

最近对话（仅用户侧）：
{history}
{last_turn_user_input_line}

当前输入：{current_input}

改写后的独立查询："""


def _has_novel_named_entity(text: str, history: List[HistoryMessage]) -> bool:
    """
    检查 text 是否引入了新话题——即查询足够长、不含依赖暗示词、
    大概率是一个自包含的新话题查询。

    不做精确 NER，用长度 + 弱依赖信号做启发式判断：
    - 查询 >= 8 字且不含任何弱依赖暗示词 -> 大概率是独立新话题
    """
    if len(text) >= 8 and not any(s in text for s in _WEAK_DEPENDENCY_HINTS):
        return True
    return False


class QueryRewriter:
    """
    两阶段查询改写器

    Stage 1 (independence_check): 保守策略，只有确认独立才跳过
    Stage 2 (rewrite):            其余一律送 LLM 改写
    """

    # 暴露给 Pipeline tracer 读取的 prompt 模板
    REWRITE_PROMPT = _REWRITE_PROMPT

    def __init__(self, local_llm_service=None):
        self._local_llm = local_llm_service

    @property
    def local_llm(self):
        if self._local_llm is None:
            from app.services.local_llm_service import get_local_llm_service
            self._local_llm = get_local_llm_service()
        return self._local_llm

    # ------------------------------------------------------------------
    # Stage 1 - Independence Check（反转默认行为）
    # ------------------------------------------------------------------

    @staticmethod
    def _is_independent(text: str, history: List[HistoryMessage]) -> bool:
        """
        判断输入是否独立于上下文。
        保守策略：只有能确认独立时才返回 True，其余一律走 LLM 改写。

        Returns:
            True = 确认独立，跳过改写
            False = 不确定或确认依赖，进入 LLM 改写
        """
        stripped = text.strip()
        lower = stripped.lower()

        # 1. 独立短表达（问候/告别等） -> 一定独立
        if lower in _STANDALONE_EXPRESSIONS:
            return True

        # 2. 含强依赖信号词 -> 一定不独立（快速路径，省掉 LLM 调用）
        for token in _DEPENDENCY_SIGNALS:
            if token in stripped:
                return False

        # 3. 含明确新话题实体（查询足够长且无依赖暗示） -> 大概率是话题切换
        if _has_novel_named_entity(stripped, history):
            return True

        # 4. 默认：不确定 -> 保守地认为依赖上下文，送 LLM 改写
        return False

    # ------------------------------------------------------------------
    # Stage 2 - Rewrite
    # ------------------------------------------------------------------

    @staticmethod
    def _format_history(
        history: List[HistoryMessage],
        max_turns: int = _REWRITE_MAX_TURNS,
    ) -> str:
        """
        仅保留最近 max_turns 轮的用户侧消息，去除助手回复噪音。
        """
        if not history:
            return "(无)"

        # 取最近 N 轮（每轮 2 条）
        recent = history[-(max_turns * 2):]

        lines = []
        for msg in recent:
            if msg.role != "user":
                continue
            lines.append(f"用户：{msg.content.strip()}")
        return "\n".join(lines) if lines else "(无)"

    @staticmethod
    def _format_last_turn_user_input(
        last_standalone_query: Optional[str] = None,
        follow_up_type: Optional[str] = None,
    ) -> str:
        """
        生成「上轮用户输入」的精简一行，供改写 prompt 使用。
        内容由 pipeline 从当前请求的 history 最后一轮用户输入传入，不塞 agent 完整回复，避免改写 LLM 长度超限。
        """
        if not last_standalone_query or not last_standalone_query.strip():
            return ""
        subject = last_standalone_query.strip()
        if len(subject) > _LAST_SUBJECT_MAX_CHARS:
            subject = subject[: _LAST_SUBJECT_MAX_CHARS] + "…"
        line = f"上一轮独立查询或用户输入（上轮主题，改写时须继承主体）：{subject}"
        if follow_up_type == "time_switch":
            line += "\n（当前为时间切换追问，须输出「时间+上轮主体」的独立查询。）"
        return line

    def rewrite(
        self,
        current_input: str,
        history: Optional[List[HistoryMessage]] = None,
        max_history_turns: int = _REWRITE_MAX_TURNS,
        category_hint: Optional[str] = None,
        last_standalone_query: Optional[str] = None,
        follow_up_type: Optional[str] = None,
    ) -> RewriteResult:
        """
        改写入口。

        Args:
            current_input: 用户当前输入
            history: 对话历史
            max_history_turns: 最大历史轮数（默认 2）
            category_hint: 分类前置时传入的领域（如 tech/economy），改写时勿偏离该领域
            last_standalone_query: 上一轮主题（由 pipeline 从当前请求 history 最后一轮用户输入取），仅短句传入
            follow_up_type: 追问类型（如 time_switch），用于 prompt 中强调时间切换须继承主体

        Returns:
            RewriteResult(standalone_query, reasoning)
        """
        current_input = current_input.strip()

        # 无历史 → 一定独立
        if not history:
            logger.debug(f"[Rewriter] 无历史，原样返回: {current_input}")
            return RewriteResult(standalone_query=current_input, reasoning=None)

        # Stage 1: Independence Check（反转默认行为：独立才跳过）
        if self._is_independent(current_input, history):
            logger.info(f"[Rewriter] 独立查询，跳过改写: {current_input}")
            return RewriteResult(standalone_query=current_input, reasoning=None)

        logger.info(f"[Rewriter] 可能依赖上下文，进入 LLM 改写: {current_input}")

        # Stage 2: LLM Rewrite
        history_text = self._format_history(history, max_history_turns)
        last_turn_user_input_line = self._format_last_turn_user_input(
            last_standalone_query=last_standalone_query,
            follow_up_type=follow_up_type,
        )
        if last_turn_user_input_line:
            last_turn_user_input_line = last_turn_user_input_line + "\n"
        category_constraint = ""
        if category_hint and category_hint.strip():
            category_constraint = (
                f"当前用户问题已被判定属于「{category_hint}」领域，"
                "改写时请勿偏离该领域，仅做指代消解与信息补全。\n\n"
            )
        prompt = _REWRITE_PROMPT.format(
            history=history_text,
            last_turn_user_input_line=last_turn_user_input_line,
            current_input=current_input,
            category_constraint=category_constraint,
        )

        try:
            if not self.local_llm.is_available:
                logger.warning("[Rewriter] 本地模型不可用，返回原输入")
                return RewriteResult(standalone_query=current_input, reasoning=None)

            raw = self.local_llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=256,
            )
            raw = raw.strip()

            # 解析：第一行为查询，第二行为「改写原因：xxx」或「无」
            lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
            if not lines:
                logger.warning("[Rewriter] 改写结果无效，返回原输入")
                return RewriteResult(standalone_query=current_input, reasoning=None)

            standalone_query = lines[0]
            # 兼容 LLM 将标签与内容写在同一行，如「改写后的独立查询：无」「第一行：火箭比赛近况」
            for prefix in ("改写后的独立查询：", "改写后的独立查询:", "第一行：", "第一行:"):
                if standalone_query.startswith(prefix):
                    standalone_query = standalone_query[len(prefix):].strip()
                    break
            if not standalone_query or standalone_query in ("无", "无。"):
                logger.warning("[Rewriter] 改写结果为无/空，返回原输入")
                return RewriteResult(standalone_query=current_input, reasoning=None)
            if len(standalone_query) > 2:
                if (standalone_query[0] == '"' and standalone_query[-1] == '"') or \
                   (standalone_query[0] == "'" and standalone_query[-1] == "'"):
                    standalone_query = standalone_query[1:-1]

            if len(standalone_query) < 2:
                logger.warning("[Rewriter] 改写结果无效，返回原输入")
                return RewriteResult(standalone_query=current_input, reasoning=None)

            reasoning = None
            if len(lines) >= 2:
                second = lines[1]
                if second == "无" or not second:
                    pass
                elif "改写原因：" in second or "原因：" in second:
                    reasoning = second.split("：", 1)[-1].strip() or None
                elif "改写原因:" in second or "原因:" in second:
                    reasoning = second.split(":", 1)[-1].strip() or None
                else:
                    reasoning = second

            logger.info(f"[Rewriter] 改写完成: '{current_input}' -> '{standalone_query}'")
            return RewriteResult(standalone_query=standalone_query, reasoning=reasoning)

        except Exception as e:
            logger.error(f"[Rewriter] 改写失败: {e}")
            return RewriteResult(standalone_query=current_input, reasoning=None)


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
_query_rewriter_instance: Optional[QueryRewriter] = None


def get_query_rewriter() -> QueryRewriter:
    """获取 QueryRewriter 单例"""
    global _query_rewriter_instance
    if _query_rewriter_instance is None:
        _query_rewriter_instance = QueryRewriter()
    return _query_rewriter_instance
