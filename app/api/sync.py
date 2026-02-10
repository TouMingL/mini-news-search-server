# app/api/sync.py
# 同步接口：用户资料 POST/GET latest、对话 GET latest（不整包 POST）、头像上传 upload_avatar

import json
import os
import time
import uuid
from flask import Blueprint, request, current_app, url_for
from app.models import db, UserProfile, Conversation, ConversationMessage, AgentSetting
from app.utils.response import success_response, error_response
from app.utils.jwt_auth import verify_token

sync_bp = Blueprint('sync', __name__, url_prefix='/api/sync')

ALLOWED_AVATAR_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp', 'gif'}
MAX_AVATAR_SIZE = 5 * 1024 * 1024  # 5MB


def _get_openid_from_token():
    """从请求头 Bearer token 解析 openid，失败返回 None"""
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return None
    try:
        token = auth_header.split(' ')[1] if ' ' in auth_header else auth_header
        return verify_token(token)
    except Exception:
        return None


def _compute_user_stats(openid):
    """从数据库实时计算用户统计：消息总数（对话次数）、使用过的智能体数"""
    from sqlalchemy import func
    # 对话次数 = 该用户的消息总条数
    message_count = ConversationMessage.query.filter_by(openid=openid).count()
    # 智能体数 = 该用户对话过的不同智能体数
    agent_count = db.session.query(
        func.count(func.distinct(Conversation.agent_ip))
    ).filter(
        Conversation.openid == openid,
        Conversation.agent_ip.isnot(None)
    ).scalar() or 0
    return agent_count, message_count


def _user_profile_to_sync_dict(profile):
    """与前端 PROFILE_DATA_KEY 一致的 JSON，含 joinDate、joinTime、selectedAgent。
    agentCount / conversationCount 从数据库实时计算，不使用存储值。"""
    agent_count, conversation_count = _compute_user_stats(profile.openid)
    join_date_str = None
    if profile.join_time:
        from datetime import datetime
        dt = datetime.utcfromtimestamp(profile.join_time / 1000.0)
        join_date_str = dt.strftime('%Y-%m-%d')
    selected_agent_val = None
    if getattr(profile, 'selected_agent', None):
        try:
            raw = profile.selected_agent
            selected_agent_val = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            pass
    return {
        'openid': profile.openid,
        'nickName': profile.nick_name,
        'avatarUrl': profile.avatar_url,
        'joinDate': join_date_str,
        'joinTime': profile.join_time,
        'agentCount': agent_count,
        'conversationCount': conversation_count,
        'selectedAgent': selected_agent_val,
    }


# ---------- 用户资料 ----------

@sync_bp.route('/upload_avatar', methods=['POST'])
def upload_avatar():
    """
    POST /api/sync/upload_avatar
    multipart/form-data，字段名 file，请求头 Authorization: Bearer <token>。
    将小程序 chooseAvatar 返回的临时文件上传到服务器，保存为静态文件，返回永久可访问 URL。
    数据库设计不变：仍用 user_profile.avatar_url 存该 URL（前端随后会调 sync user_profile 写入）。
    """
    openid = _get_openid_from_token()
    if not openid:
        return error_response('未授权，请先登录', 401)

    file = request.files.get('file')
    if not file or file.filename == '':
        return error_response('请选择头像文件', 400)

    ext = (file.filename.rsplit('.', 1)[-1] or '').lower()
    if ext not in ALLOWED_AVATAR_EXTENSIONS:
        return error_response('仅支持 jpg/png/webp/gif', 400)

    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_AVATAR_SIZE:
        return error_response('头像大小不能超过 5MB', 400)

    upload_dir = os.path.join(current_app.root_path, 'static', 'avatars')
    os.makedirs(upload_dir, exist_ok=True)
    safe_name = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.{ext}"
    save_path = os.path.join(upload_dir, safe_name)
    try:
        file.save(save_path)
    except OSError as e:
        current_app.logger.exception(e)
        return error_response('保存文件失败', 500)

    base_url = request.host_url.rstrip('/')
    rel_path = url_for('static', filename=f'avatars/{safe_name}')
    permanent_url = base_url + rel_path
    current_app.logger.info(f'头像已保存: openid={openid[:10]}..., url={permanent_url}')
    return success_response({'avatarUrl': permanent_url, 'avatar': permanent_url})


@sync_bp.route('/user_profile', methods=['POST'])
def sync_user_profile():
    """
    POST /api/sync/user_profile
    请求体：{ nickName, avatarUrl, joinTime, agentCount?, conversationCount? }
    openid 由 JWT 得到，不信任 body。
    """
    openid = _get_openid_from_token()
    if not openid:
        return error_response('未授权，请先登录', 401)

    data = request.get_json()
    if not data:
        return error_response('请求体不能为空', 400)

    nick_name = data.get('nickName') or data.get('nick_name')
    if not nick_name or not isinstance(nick_name, str):
        nick_name = '用户'
    avatar_url = data.get('avatarUrl') or data.get('avatar_url')
    join_time = data.get('joinTime') or data.get('join_time')
    if join_time is None:
        return error_response('joinTime 必填', 400)
    try:
        join_time = int(join_time)
    except (TypeError, ValueError):
        return error_response('joinTime 必须为毫秒时间戳', 400)

    selected_agent_raw = data.get('selectedAgent')
    selected_agent_str = None
    if selected_agent_raw is not None:
        if selected_agent_raw is None or (isinstance(selected_agent_raw, dict) and not selected_agent_raw):
            selected_agent_str = None
        else:
            try:
                selected_agent_str = json.dumps(selected_agent_raw) if isinstance(selected_agent_raw, dict) else None
            except (TypeError, ValueError):
                selected_agent_str = None

    try:
        profile = UserProfile.query.filter_by(openid=openid).first()
        now_ms = int(time.time() * 1000)
        # agentCount / conversationCount 由服务端从 DB 实时计算，不信任客户端传值
        real_agent_count, real_conversation_count = _compute_user_stats(openid)
        if not profile:
            profile = UserProfile(
                openid=openid,
                nick_name=nick_name,
                avatar_url=avatar_url or None,
                join_time=join_time,
                agent_count=real_agent_count,
                conversation_count=real_conversation_count,
                updated_at=now_ms,
                selected_agent=selected_agent_str,
            )
            db.session.add(profile)
        else:
            profile.nick_name = nick_name
            profile.avatar_url = avatar_url or profile.avatar_url
            profile.join_time = join_time
            profile.agent_count = real_agent_count
            profile.conversation_count = real_conversation_count
            profile.updated_at = now_ms
            if 'selectedAgent' in data:
                profile.selected_agent = selected_agent_str
        db.session.commit()
        return success_response(_user_profile_to_sync_dict(profile))
    except Exception as e:
        current_app.logger.exception(e)
        db.session.rollback()
        return error_response('保存用户资料失败', 500)


@sync_bp.route('/user_profile/latest', methods=['GET'])
def get_user_profile_latest():
    """
    GET /api/sync/user_profile/latest
    返回与前端本地一致的 profile JSON，供 wx.setStorageSync(PROFILE_DATA_KEY, data)。
    """
    openid = _get_openid_from_token()
    if not openid:
        return error_response('未授权，请先登录', 401)

    profile = UserProfile.query.filter_by(openid=openid).first()
    if not profile:
        agent_count, conversation_count = _compute_user_stats(openid)
        now_ms = int(time.time() * 1000)
        from datetime import datetime
        join_date_str = datetime.utcfromtimestamp(now_ms / 1000.0).strftime('%Y-%m-%d')
        return success_response({
            'openid': openid,
            'nickName': '用户',
            'avatarUrl': None,
            'joinDate': join_date_str,
            'joinTime': now_ms,
            'agentCount': agent_count,
            'conversationCount': conversation_count,
            'selectedAgent': None,
        })
    return success_response(_user_profile_to_sync_dict(profile))


# ---------- Agent 设置（URL、api_key 等） ----------

MAX_AGENT_SETTINGS_PER_USER = 50


@sync_bp.route('/agent_settings/latest', methods=['GET'])
def get_agent_settings_latest():
    """
    GET /api/sync/agent_settings/latest
    返回该用户全部 agent 设置：{ "settings": { "agentId": { "baseUrl", "apiKey", "model?", "chatPath?" }, ... } }
    """
    openid = _get_openid_from_token()
    if not openid:
        return error_response('未授权，请先登录', 401)
    try:
        rows = AgentSetting.query.filter_by(openid=openid).all()
        settings = {}
        for row in rows:
            settings[str(row.agent_id)] = {
                'baseUrl': row.base_url or '',
                'apiKey': row.api_key or '',
                'model': row.model if row.model else None,
                'chatPath': row.chat_path if row.chat_path else None,
            }
        return success_response({'settings': settings})
    except Exception as e:
        current_app.logger.exception(e)
        return error_response('拉取 agent 设置失败', 500)


@sync_bp.route('/agent_settings', methods=['POST'])
def sync_agent_settings():
    """
    POST /api/sync/agent_settings
    请求体：{ "settings": { "agentId": { "baseUrl", "apiKey", "model?", "chatPath?" }, ... } }
    对该用户先删后插，使服务端与提交的 map 一致。
    """
    openid = _get_openid_from_token()
    if not openid:
        return error_response('未授权，请先登录', 401)
    data = request.get_json()
    if not data or not isinstance(data.get('settings'), dict):
        return error_response('请求体需包含 settings 对象', 400)
    settings = data['settings']
    if len(settings) > MAX_AGENT_SETTINGS_PER_USER:
        return error_response(f'单用户最多保存 {MAX_AGENT_SETTINGS_PER_USER} 条 agent 设置', 400)
    now_ms = int(time.time() * 1000)
    try:
        AgentSetting.query.filter_by(openid=openid).delete()
        for agent_id, cfg in settings.items():
            if not isinstance(cfg, dict):
                continue
            base_url = (cfg.get('baseUrl') or cfg.get('base_url') or '').strip()
            if not base_url:
                continue
            api_key = (cfg.get('apiKey') or cfg.get('api_key') or '')
            if api_key is not None:
                api_key = str(api_key)
            model = cfg.get('model')
            if model is not None:
                model = str(model).strip() or None
            chat_path = cfg.get('chatPath') or cfg.get('chat_path')
            if chat_path is not None:
                chat_path = str(chat_path).strip() or None
            row = AgentSetting(
                openid=openid,
                agent_id=str(agent_id),
                base_url=base_url,
                api_key=api_key,
                model=model,
                chat_path=chat_path,
                updated_at=now_ms,
            )
            db.session.add(row)
        db.session.commit()
        rows = AgentSetting.query.filter_by(openid=openid).all()
        out_settings = {}
        for row in rows:
            out_settings[str(row.agent_id)] = {
                'baseUrl': row.base_url or '',
                'apiKey': row.api_key or '',
                'model': row.model if row.model else None,
                'chatPath': row.chat_path if row.chat_path else None,
            }
        return success_response({'settings': out_settings})
    except Exception as e:
        current_app.logger.exception(e)
        db.session.rollback()
        return error_response('保存 agent 设置失败', 500)


# ---------- 对话与消息（仅 GET latest，不整包 POST） ----------

# 同步时每条对话最多带回的消息条数（0=不带消息，仅列表；业界：本地只缓存最近 N 条）
DEFAULT_MESSAGES_PER_CONVERSATION = 50

@sync_bp.route('/conversations/latest', methods=['GET'])
def get_conversations_latest():
    """
    GET /api/sync/conversations/latest?messagesPerConversation=50
    按 openid 查该用户所有对话；messagesPerConversation=0 时不带消息（仅列表），>0 时每条对话只带最近 N 条消息（控制本地存储）。
    返回：{ conversations: [ { id, title, preview, updatedAt, createdAt, messages?: [...] } ] }
    """
    openid = _get_openid_from_token()
    if not openid:
        return error_response('未授权，请先登录', 401)

    messages_per_conv = request.args.get('messagesPerConversation', DEFAULT_MESSAGES_PER_CONVERSATION, type=int)
    messages_per_conv = max(0, min(messages_per_conv, 100))

    try:
        convs = (
            Conversation.query.filter_by(openid=openid)
            .order_by(Conversation.updated_at.desc())
            .all()
        )
        result = []
        for c in convs:
            agent_val = None
            if getattr(c, 'agent_snapshot', None):
                try:
                    raw = c.agent_snapshot
                    agent_val = json.loads(raw) if isinstance(raw, str) else raw
                except (TypeError, ValueError):
                    pass
            item = {
                'id': c.chat_id,
                'title': c.title,
                'preview': c.preview or '',
                'updatedAt': c.updated_at,
                'createdAt': c.created_at,
                'agent_ip': c.agent_ip if c.agent_ip is not None else None,
                'agent': agent_val,
            }
            if messages_per_conv > 0:
                msgs = (
                    ConversationMessage.query.filter_by(conversation_id=c.chat_id)
                    .order_by(ConversationMessage.created_at.desc())
                    .limit(messages_per_conv)
                    .all()
                )
                messages = [
                    {
                        'id': m.message_id,
                        'type': 'ai' if m.speaker == 'agent' else 'user',
                        'content': m.content,
                        'timestamp': m.created_at,
                    }
                    for m in reversed(msgs)
                ]
                item['messages'] = messages
            else:
                item['messages'] = []
            result.append(item)
        return success_response({'conversations': result})
    except Exception as e:
        current_app.logger.exception(e)
        return error_response('拉取对话列表失败', 500)
