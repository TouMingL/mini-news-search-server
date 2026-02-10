# app/services/query_rewriter.py
"""
预处理层 - Query Rewriter（两阶段设计）

Stage 1 - Independence Check（纯规则，无 LLM 调用）:
    判断当前输入是 topic-shift（独立查询）还是 context-dependent（上下文依赖）。
    只有 context-dependent 才进入 Stage 2。

Stage 2 - Rewrite（调用本地 LLM）:
    使用最近 2 轮历史（仅用户侧）改写为独立查询。
"""
from typing import List, Optional
from loguru import logger

from app.services.schemas import HistoryMessage


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

# 指代词 —— 出现任意一个即判定为 context-dependent
_PRONOUNS = ('它', '这个', '那个', '这', '那', '他们', '她们', '它们')

# 省略表达
_ELLIPSIS = ('继续', '还有', '然后呢', '接着', '再说说', '详细', '更多', '还有呢', '呢')

# 精炼 / 排除意图（需结合上文改写）
_REFINEMENT = ('不要', '不看', '换一个', '换一批', '其他的', '排除', '别推', '换成', '除了')

# 改写时使用的最大历史轮数（1 轮 = 1 user + 1 assistant）
_REWRITE_MAX_TURNS = 2

# 改写 Prompt —— 规则 1 就是"新话题直接原样返回"
_REWRITE_PROMPT = """你是一个查询改写助手。将用户当前输入改写为一个独立、无需上下文即可理解的查询。

规则（按优先级排列）：
1. 若当前输入引入了全新话题（历史中未出现的实体/领域），直接原样返回，不要用历史覆盖
2. 消解指代词（它/这个/那个 -> 具体实体名称）
3. 补全省略信息（"继续" -> "继续[具体内容]"）
4. 精炼/排除意图：用户说「不要XX」「换一个」时，结合最近一轮主题改写。例如上一轮问「体育新闻」，当前说「不要足球」-> 「体育新闻 排除足球」
5. 保持原意，不添加推测
6. 只输出改写后的查询，不要任何解释

最近对话（仅用户侧）：
{history}

当前输入：{current_input}

改写后的独立查询："""


class QueryRewriter:
    """
    两阶段查询改写器

    Stage 1 (independence_check): 纯规则判定，不调用 LLM
    Stage 2 (rewrite):            仅对 context-dependent 的输入调用 LLM 改写
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
    # Stage 1 - Independence Check
    # ------------------------------------------------------------------

    @staticmethod
    def _has_context_dependency(text: str) -> str:
        """
        判断输入是否依赖上下文。

        Returns:
            依赖原因字符串（非空 = 需要改写），空字符串 = 独立查询
        """
        stripped = text.strip()
        lower = stripped.lower()

        # 1. 独立短表达 → 永远独立
        if lower in _STANDALONE_EXPRESSIONS:
            return ""

        # 2. 含精炼/排除词 → 依赖上文
        for r in _REFINEMENT:
            if r in stripped:
                return f"refinement:{r}"

        # 3. 含指代词 → 依赖上文
        for p in _PRONOUNS:
            if p in stripped:
                return f"pronoun:{p}"

        # 4. 含省略表达 → 依赖上文
        for e in _ELLIPSIS:
            if e in lower:
                return f"ellipsis:{e}"

        # 5. 独立查询（不再用「短于 N 字就改写」的兜底规则）
        return ""

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

    def rewrite(
        self,
        current_input: str,
        history: Optional[List[HistoryMessage]] = None,
        max_history_turns: int = _REWRITE_MAX_TURNS,
    ) -> str:
        """
        改写入口。

        Args:
            current_input: 用户当前输入
            history: 对话历史
            max_history_turns: 最大历史轮数（默认 2）

        Returns:
            独立完整的查询字符串
        """
        current_input = current_input.strip()

        # 无历史 → 一定独立
        if not history:
            logger.debug(f"[Rewriter] 无历史，原样返回: {current_input}")
            return current_input

        # Stage 1: Independence Check
        dep_reason = self._has_context_dependency(current_input)
        if not dep_reason:
            logger.info(f"[Rewriter] 独立查询，跳过改写: {current_input}")
            return current_input

        logger.info(f"[Rewriter] 上下文依赖 ({dep_reason})，进入改写: {current_input}")

        # Stage 2: LLM Rewrite
        history_text = self._format_history(history, max_history_turns)
        prompt = _REWRITE_PROMPT.format(
            history=history_text,
            current_input=current_input,
        )

        try:
            if not self.local_llm.is_available:
                logger.warning("[Rewriter] 本地模型不可用，返回原输入")
                return current_input

            rewritten = self.local_llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=256,
            )
            rewritten = rewritten.strip()

            # 基本校验
            if not rewritten or len(rewritten) < 2:
                logger.warning("[Rewriter] 改写结果无效，返回原输入")
                return current_input

            # 去引号包裹
            if len(rewritten) > 2:
                if (rewritten[0] == '"' and rewritten[-1] == '"') or \
                   (rewritten[0] == "'" and rewritten[-1] == "'"):
                    rewritten = rewritten[1:-1]

            logger.info(f"[Rewriter] 改写完成: '{current_input}' -> '{rewritten}'")
            return rewritten

        except Exception as e:
            logger.error(f"[Rewriter] 改写失败: {e}")
            return current_input


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
