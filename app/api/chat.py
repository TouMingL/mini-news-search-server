# app/api/chat.py
"""
Unified Chat Gateway - 统一对话入口
前端只调此接口，后端 Pipeline 全权负责意图判断、路由决策、执行生成。
"""
import json
from flask import Blueprint, request, Response, stream_with_context
from app.utils.response import error_response
from app.utils.jwt_auth import verify_token
from loguru import logger

chat_bp = Blueprint('chat', __name__, url_prefix='/api')

# Pipeline 实例（延迟初始化）
_pipeline = None


def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        from app.services.pipeline import get_pipeline
        _pipeline = get_pipeline()
    return _pipeline


def _get_openid_from_token():
    """从请求头中获取并验证JWT token，返回openid"""
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return None
    try:
        token = auth_header.split(' ')[1] if ' ' in auth_header else auth_header
        return verify_token(token)
    except Exception:
        return None


@chat_bp.route('/chat', methods=['POST'])
def unified_chat():
    """
    POST /api/chat
    统一对话入口 —— 前端唯一需要调用的对话接口。

    后端 Pipeline 自动完成：
      查询改写 -> 意图分类 -> 路由决策 -> 检索(可选) -> LLM 生成

    请求体：
    {
        "query": "用户消息",
        "conversation_id": "chat_xxx",   // 可选，用于多轮对话
        "history_turns": 5               // 可选，获取最近N轮历史，默认5
    }

    响应：SSE 流 (text/event-stream)
    每行格式: data: <json>\n\n
      - 内容块:   {"choices":[{"delta":{"content":"..."}}]}
      - 替换事件: {"replace":"..."}
      - 结束事件: {"sources":[...], "done":true}
    """
    # 鉴权
    openid = _get_openid_from_token()
    if not openid:
        return error_response('未授权，请先登录', 401)

    data = request.get_json()
    if not data:
        return error_response('请求体不能为空', 400)

    query = (data.get('query') or '').strip()
    if not query:
        return error_response('query不能为空', 400)

    conversation_id = data.get('conversation_id')
    history_turns = data.get('history_turns', 5)
    if not isinstance(history_turns, int) or history_turns < 0:
        history_turns = 5
    deep_think = bool(data.get('deep_think', False))

    try:
        from app.services.schemas import PipelineInput

        pipeline = _get_pipeline()
        input_data = PipelineInput(
            query=query,
            conversation_id=conversation_id,
            history_turns=history_turns,
            deep_think=deep_think,
        )

        def generate():
            for event in pipeline.run_stream(input_data):
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    except Exception as e:
        logger.error(f"统一对话接口失败: {e}")
        return error_response(f'对话请求失败: {str(e)}', 500)
