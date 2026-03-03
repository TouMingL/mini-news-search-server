# app/api/conversations.py
# 对话落库：聊天接口返回答复后调用，将本条 user + agent 消息写入 DB（不整包 sync）
# 支持按时间戳分页拉取历史消息（业界：cursor/timestamp 分页）

import json
import time
from flask import Blueprint, request, current_app
from app.models import db, Conversation, ConversationMessage
from app.utils.response import success_response, error_response
from app.utils.jwt_auth import verify_token

conversations_bp = Blueprint('conversations', __name__, url_prefix='/api/conversations')

DEFAULT_MESSAGES_LIMIT = 20
MAX_MESSAGES_LIMIT = 100


def _get_openid_from_token():
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return None
    try:
        token = auth_header.split(' ')[1] if ' ' in auth_header else auth_header
        return verify_token(token)
    except Exception:
        return None


def _make_message_id():
    return str(int(time.time() * 1000) * 1000 + __import__('random').randint(0, 999))


@conversations_bp.route('/<chat_id>/messages', methods=['GET'])
def list_messages(chat_id):
    """
    GET /api/conversations/<chat_id>/messages?before=<timestamp>&limit=20
    分页拉取历史消息（before 为游标：只返回 created_at < before 的旧消息，用于「加载更多」）。
    返回：{ messages: [ { id, type, content, timestamp } ], hasMore: bool }
    """
    openid = _get_openid_from_token()
    if not openid:
        return error_response('未授权，请先登录', 401)
    if not chat_id or not chat_id.strip():
        return error_response('chat_id 不能为空', 400)

    before = request.args.get('before', type=int)
    limit = request.args.get('limit', DEFAULT_MESSAGES_LIMIT, type=int)
    limit = min(max(1, limit), MAX_MESSAGES_LIMIT)

    try:
        q = ConversationMessage.query.filter(
            ConversationMessage.conversation_id == chat_id,
            ConversationMessage.openid == openid,
        )
        if before is not None:
            q = q.filter(ConversationMessage.created_at < before)
        q = q.order_by(ConversationMessage.created_at.desc()).limit(limit + 1)
        rows = q.all()
        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]
        messages = [
            {
                'id': m.message_id,
                'type': 'ai' if m.speaker == 'agent' else 'user',
                'content': m.content,
                'timestamp': m.created_at,
            }
            for m in reversed(rows)
        ]
        return success_response({'messages': messages, 'hasMore': has_more})
    except Exception as e:
        current_app.logger.exception(e)
        return error_response('拉取消息失败', 500)


@conversations_bp.route('/<chat_id>/messages', methods=['POST'])
def append_messages(chat_id):
    """
    POST /api/conversations/<chat_id>/messages
    聊天接口返回答复后调用，将本条 user 消息 + agent 答复写入 DB。
    请求体：{ userContent, agentContent, userMessageId?, agentMessageId?, title? }
    openid 由 JWT 得到。
    """
    openid = _get_openid_from_token()
    if not openid:
        return error_response('未授权，请先登录', 401)

    if not chat_id or not chat_id.strip():
        return error_response('chat_id 不能为空', 400)

    data = request.get_json()
    if not data:
        return error_response('请求体不能为空', 400)

    user_content = data.get('userContent') or data.get('user_content') or ''
    agent_content = data.get('agentContent') or data.get('agent_content') or ''
    if not user_content and not agent_content:
        return error_response('userContent 与 agentContent 至少填一个', 400)

    user_message_id = data.get('userMessageId') or data.get('user_message_id') or _make_message_id()
    agent_message_id = data.get('agentMessageId') or data.get('agent_message_id') or _make_message_id()
    title = data.get('title') or '新对话'
    agent_snapshot_raw = data.get('agentSnapshot') or data.get('agent_snapshot')
    now_ms = int(time.time() * 1000)

    agent_snapshot_str = None
    agent_ip_from_snapshot = None
    if agent_snapshot_raw and isinstance(agent_snapshot_raw, dict) and agent_snapshot_raw.get('id') is not None:
        try:
            agent_snapshot_str = json.dumps(agent_snapshot_raw)
            api_base = agent_snapshot_raw.get('apiBaseUrl') or ''
            if isinstance(api_base, str) and api_base:
                base = api_base.replace('https://', '').replace('http://', '').strip()
                agent_ip_from_snapshot = (base.split('/')[0].split(':')[0] or None)
        except (TypeError, ValueError):
            pass

    try:
        conv = Conversation.query.filter_by(chat_id=chat_id, openid=openid).first()
        if not conv:
            conv = Conversation(
                openid=openid,
                chat_id=chat_id,
                title=title[:256] if title else '新对话',
                preview=(agent_content or user_content)[:512] if (agent_content or user_content) else '',
                created_at=now_ms,
                updated_at=now_ms,
                agent_ip=agent_ip_from_snapshot,
                agent_snapshot=agent_snapshot_str,
            )
            db.session.add(conv)
        else:
            conv.updated_at = now_ms
            if title:
                conv.title = title[:256]
            if agent_content or user_content:
                conv.preview = (agent_content or user_content)[:512]
            # 业界惯例：会话元数据（含绑定的 agent）仅在创建时写入，后续消息不再覆盖

        if user_content:
            existing_user = ConversationMessage.query.filter_by(message_id=user_message_id).first()
            if not existing_user:
                db.session.add(ConversationMessage(
                    openid=openid,
                    conversation_id=chat_id,
                    message_id=user_message_id,
                    speaker='user',
                    content=user_content,
                    created_at=now_ms,
                ))
        if agent_content:
            existing_agent = ConversationMessage.query.filter_by(message_id=agent_message_id).first()
            if not existing_agent:
                db.session.add(ConversationMessage(
                    openid=openid,
                    conversation_id=chat_id,
                    message_id=agent_message_id,
                    speaker='agent',
                    content=agent_content,
                    created_at=now_ms,
                ))

        db.session.commit()
        return success_response({'ok': True})
    except Exception as e:
        current_app.logger.exception(e)
        db.session.rollback()
        return error_response('写入对话失败', 500)


@conversations_bp.route('/<chat_id>/messages/delete', methods=['POST'])
def delete_messages(chat_id):
    """
    POST /api/conversations/<chat_id>/messages/delete
    静默删除指定消息：按 conversation_id + message_id 列表查找并删除对应行。
    请求体：{ messageIds: ["id1", "id2", ...] }
    openid 由 JWT 得到，仅删除当前用户的记录。
    """
    openid = _get_openid_from_token()
    if not openid:
        return error_response('未授权，请先登录', 401)

    if not chat_id or not chat_id.strip():
        return error_response('chat_id 不能为空', 400)

    data = request.get_json()
    if not data:
        return error_response('请求体不能为空', 400)

    message_ids = data.get('messageIds') or data.get('message_ids') or []
    if not isinstance(message_ids, list):
        return error_response('messageIds 必须为数组', 400)
    message_ids = [str(mid).strip() for mid in message_ids if mid is not None and str(mid).strip()]

    if not message_ids:
        return success_response({'ok': True, 'deleted': 0})

    try:
        deleted = ConversationMessage.query.filter(
            ConversationMessage.conversation_id == chat_id,
            ConversationMessage.openid == openid,
            ConversationMessage.message_id.in_(message_ids),
        ).delete(synchronize_session=False)
        db.session.commit()
        return success_response({'ok': True, 'deleted': deleted})
    except Exception as e:
        current_app.logger.exception(e)
        db.session.rollback()
        return error_response('删除消息失败', 500)


@conversations_bp.route('/<chat_id>/delete', methods=['POST'])
def delete_conversation(chat_id):
    """
    POST /api/conversations/<chat_id>/delete
    静默删除对话：先删该对话下所有消息（conversation_messages），再删对话行（conversations）。
    openid 由 JWT 得到，仅删除当前用户的记录。
    """
    openid = _get_openid_from_token()
    if not openid:
        return error_response('未授权，请先登录', 401)

    if not chat_id or not chat_id.strip():
        return error_response('chat_id 不能为空', 400)

    try:
        # 先删消息表：该对话下、当前用户的所有消息
        deleted_messages = ConversationMessage.query.filter(
            ConversationMessage.conversation_id == chat_id,
            ConversationMessage.openid == openid,
        ).delete(synchronize_session=False)
        # 再删对话表：该对话行（且属当前用户）
        deleted_conv = Conversation.query.filter(
            Conversation.chat_id == chat_id,
            Conversation.openid == openid,
        ).delete(synchronize_session=False)
        db.session.commit()
        return success_response({'ok': True, 'deletedMessages': deleted_messages, 'deletedConversation': deleted_conv})
    except Exception as e:
        current_app.logger.exception(e)
        db.session.rollback()
        return error_response('删除对话失败', 500)
