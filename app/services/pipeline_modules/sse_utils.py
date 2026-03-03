# app/services/pipeline_modules/sse_utils.py
"""
SSE 事件工具与对话历史提取工具。

产品场景：流式回复发送到小程序前做安全显示过滤（去除乱码字符），
以及从对话历史中提取上一轮用户输入供查询改写使用。
"""
from typing import Dict, List, Optional, Any

from app.utils.text_encoding import safe_for_display


def _sanitize_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """对发往小程序的事件做安全显示过滤，避免 ◇ 等乱码。"""
    out = dict(event)
    if "choices" in out and out["choices"]:
        delta = (out["choices"][0].get("delta") or {}).copy()
        if "content" in delta and isinstance(delta["content"], str):
            delta["content"] = safe_for_display(delta["content"])
        out["choices"] = [{"delta": delta}]
    if "replace" in out and isinstance(out["replace"], str):
        out["replace"] = safe_for_display(out["replace"])
    if "sources" in out:
        out["sources"] = [
            {
                k: safe_for_display(v) if isinstance(v, str) else v
                for k, v in (src if isinstance(src, dict) else {}).items()
            }
            for src in out["sources"]
        ]
    return out


def _get_last_turn_user_input_from_history(
    history: Optional[List[Any]],
) -> Optional[str]:
    """
    改写用的「上轮用户输入」：仅从当前请求所带对话历史的最后一轮用户输入取。
    不依赖 state，用户删掉上数轮 Q&A 后客户端发来的 history 变短，「上一轮」自然对齐。
    """
    if not history:
        return None
    for msg in reversed(history):
        if getattr(msg, "role", None) == "user" and getattr(msg, "content", None):
            s = (msg.content or "").strip()
            if s:
                return s[:80] + "\u2026" if len(s) > 80 else s
    return None
