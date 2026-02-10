# app/services/pipeline_tracer.py
"""
Pipeline 全流程 Trace 日志
每次 pipeline 执行生成一个独立文件，记录两个模型（Qwen / GLM）的完整上下文，
便于逐条对比「模型看到了什么」vs「模型输出了什么」。
"""
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from loguru import logger


class PipelineTracer:
    """
    单次 Pipeline 执行的 trace 记录器。
    用法：
        tracer = PipelineTracer(request_id)
        tracer.record_input(...)
        tracer.record_rewrite(...)
        ...
        tracer.flush()   # 写入文件
    """

    _SEP = "=" * 80
    _SUB = "-" * 80

    def __init__(self, request_id: str, log_dir: str = "logs/pipeline"):
        self.request_id = request_id
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lines: List[str] = []
        self._start_ts = datetime.now()
        # 文件名：时间戳_requestId.txt —— 每个文件只存一次流程
        ts = self._start_ts.strftime("%Y%m%d_%H%M%S")
        self._filename = self.log_dir / f"trace_{ts}_{request_id}.txt"
        self._header()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _w(self, text: str = ""):
        self._lines.append(text)

    def _header(self):
        self._w(self._SEP)
        self._w(f"Pipeline Trace: {self.request_id}")
        self._w(f"Time: {self._start_ts.strftime('%Y-%m-%d %H:%M:%S.%f')}")
        self._w(self._SEP)
        self._w()

    # ------------------------------------------------------------------
    # 对外记录接口
    # ------------------------------------------------------------------

    def record_input(
        self,
        raw_query: str,
        conversation_id: Optional[str],
        history: Optional[List[Any]] = None,
    ):
        """[STEP 0] 原始输入 + 对话历史"""
        self._w(f"[STEP 0] 原始输入")
        self._w(self._SUB)
        self._w(f"Conversation ID : {conversation_id or '(anonymous)'}")
        self._w(f"用户输入        : {raw_query}")
        self._w()
        if history:
            self._w("对话历史:")
            for msg in history:
                role = getattr(msg, "role", "?")
                content = getattr(msg, "content", str(msg))
                tag = "用户" if role == "user" else "助手"
                # 助手回复截取前200字，避免日志过长
                if role == "assistant" and len(content) > 200:
                    content = content[:200] + "...(truncated)"
                self._w(f"  [{tag}] {content}")
        else:
            self._w("对话历史: (无)")
        self._w()

    def record_rewrite(
        self,
        prompt: str,
        result: str,
        elapsed_ms: float,
        skipped: bool = False,
    ):
        """[STEP 1] 查询改写 (Qwen)"""
        self._w(f"[STEP 1] 查询改写 (Qwen) | 耗时 {elapsed_ms:.1f}ms")
        self._w(self._SUB)
        if skipped:
            self._w("(跳过改写，原样返回)")
            self._w(f"结果: {result}")
        else:
            self._w(">>> Qwen Prompt >>>")
            self._w(prompt)
            self._w("<<< Qwen Output <<<")
            self._w(result)
        self._w()

    def record_classify(
        self,
        prompt: str,
        result_dict: Dict[str, Any],
        elapsed_ms: float,
    ):
        """[STEP 2] 意图分类 (Qwen)"""
        self._w(f"[STEP 2] 意图分类 (Qwen) | 耗时 {elapsed_ms:.1f}ms")
        self._w(self._SUB)
        self._w(">>> Qwen Prompt >>>")
        self._w(prompt)
        self._w("<<< Qwen Output <<<")
        for k, v in result_dict.items():
            self._w(f"  {k}: {v}")
        self._w()

    def record_route(
        self,
        action: str,
        reason: str,
        elapsed_ms: float,
    ):
        """[STEP 3] 路由决策"""
        self._w(f"[STEP 3] 路由决策 | 耗时 {elapsed_ms:.1f}ms")
        self._w(self._SUB)
        self._w(f"Action : {action}")
        self._w(f"Reason : {reason}")
        self._w()

    def record_search(
        self,
        search_queries: List[str],
        results: List[Dict[str, Any]],
        fallback_used: bool = False,
        anchor_date: Optional[Any] = None,
        half_life_days: Optional[float] = None,
    ):
        """[STEP 4] 检索结果（含新闻正文全文 + 时间衰减信息）"""
        self._w(f"[STEP 4] 检索结果 | 共 {len(results)} 条 | fallback={fallback_used}")
        self._w(self._SUB)
        self._w(f"检索查询: {search_queries}")
        if anchor_date is not None:
            anchor_str = anchor_date.strftime("%Y-%m-%d") if hasattr(anchor_date, "strftime") else str(anchor_date)
            self._w(f"时间衰减: anchor_date={anchor_str}, half_life={half_life_days}d")
        self._w()
        for i, item in enumerate(results, 1):
            self._w(f"--- 第 {i} 条 ---")
            orig_score = item.get("original_score")
            tw = item.get("time_weight")
            if orig_score is not None:
                self._w(f"  score          : {item.get('score', 0):.4f}  (semantic={orig_score:.4f} x time_weight={tw})")
            else:
                self._w(f"  score          : {item.get('score', 0):.4f}")
            self._w(f"  source         : {item.get('source', '')}")
            self._w(f"  title          : {item.get('title', '')}")
            self._w(f"  published_time : {item.get('published_time', '')}")
            self._w(f"  category       : {item.get('category', '')}")
            self._w(f"  link           : {item.get('link', '')}")
            # 完整正文 —— 这是核心：对比「模型看到了什么」
            content = item.get("content", "")
            self._w(f"  content (len={len(content)}):")
            self._w(content if content else "(空)")
            self._w()
        self._w()

    def record_glm_prompt(
        self,
        system_prompt: str,
        user_prompt: str,
    ):
        """[STEP 5a] GLM 完整 Prompt"""
        self._w(f"[STEP 5] LLM 回答生成 (GLM)")
        self._w(self._SUB)
        self._w(">>> System Prompt >>>")
        self._w(system_prompt)
        self._w()
        self._w(">>> User Prompt (含 Read-again 重复) >>>")
        self._w(user_prompt)
        self._w()

    def record_glm_output(self, answer: str, verified: bool = True):
        """[STEP 5b] GLM 完整输出 + 核查结果"""
        self._w("<<< GLM Output <<<")
        self._w(answer if answer else "(空)")
        self._w()
        self._w(f"事实核查通过: {'YES' if verified else 'NO (已替换为保守回复)'}")
        self._w()

    def record_direct_generate(
        self,
        messages: List[Dict[str, str]],
        answer: str,
    ):
        """[STEP 5-direct] 直接生成（非 RAG 路径）"""
        self._w(f"[STEP 5] 直接生成 (GLM, 非 RAG)")
        self._w(self._SUB)
        self._w(">>> Messages >>>")
        for msg in messages:
            self._w(f"  [{msg.get('role', '?')}]")
            self._w(f"  {msg.get('content', '')}")
            self._w()
        self._w("<<< GLM Output <<<")
        self._w(answer if answer else "(空)")
        self._w()

    def record_error(self, error: str):
        """记录错误"""
        self._w(f"[ERROR]")
        self._w(self._SUB)
        self._w(error)
        self._w()

    # ------------------------------------------------------------------
    # 写入文件
    # ------------------------------------------------------------------

    def flush(self, total_ms: float = 0):
        """写入磁盘。调用一次后不可再写。"""
        self._w(self._SEP)
        self._w(f"Pipeline 完成 | 总耗时: {total_ms:.1f}ms")
        self._w(self._SEP)

        text = "\n".join(self._lines)
        try:
            self._filename.write_text(text, encoding="utf-8")
            logger.info(f"Pipeline trace 已写入: {self._filename}")
        except Exception as e:
            logger.error(f"写入 trace 文件失败: {e}")
