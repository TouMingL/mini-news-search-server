# app/api/notify.py
# 订阅消息等通知下发（需后端调用微信接口）

from flask import Blueprint, request, current_app
from app.utils.response import success_response, error_response
from app.utils.jwt_auth import verify_token
from app.utils.wechat import send_subscribe_message
from app.utils.exceptions import WeChatAPIError, WeChatConfigError, WeChatNetworkError

notify_bp = Blueprint('notify', __name__, url_prefix='/api/notify')


def _get_openid_from_token():
    auth = request.headers.get('Authorization')
    if not auth or ' ' not in auth:
        return None
    try:
        token = auth.split(' ', 1)[1]
        return verify_token(token)
    except Exception:
        return None


@notify_bp.route('/subscribe', methods=['POST'])
def send_subscribe():
    """
    向当前登录用户发送一次性订阅消息（后端调用微信接口下发）。
    请求体: { "template_id": "xxx", "data": { "key1": { "value": "v1" }, ... }, "page": "pages/chat/chat" (可选) }
    """
    openid = _get_openid_from_token()
    if not openid:
        return error_response('未授权，请先登录', 401)
    body = request.get_json() or {}
    template_id = body.get('template_id')
    data = body.get('data')
    if not template_id or not data or not isinstance(data, dict):
        return error_response('缺少 template_id 或 data')
    page = body.get('page')
    try:
        result = send_subscribe_message(openid, template_id, data, page=page)
        return success_response(result)
    except WeChatConfigError as e:
        return error_response(str(e), 500)
    except WeChatAPIError as e:
        current_app.logger.warning('订阅消息下发失败: %s (errcode=%s)', e.errmsg, getattr(e, 'errcode', None))
        return error_response(e.errmsg or '发送失败', 400)
    except WeChatNetworkError as e:
        return error_response('网络异常，请稍后重试', 502)
