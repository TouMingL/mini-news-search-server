# app/api/user.py
# 用户信息相关接口

from flask import Blueprint, request, current_app
from app.models import db, UserProfile
from app.utils.response import success_response, error_response
from app.utils.jwt_auth import verify_token

user_bp = Blueprint('user', __name__, url_prefix='/api/user')


def get_current_user():
    """
    从请求头获取token并验证，返回当前用户
    
    Returns:
        UserProfile: 用户对象，如果验证失败返回None
    """
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return None
    
    # 提取token (格式: Bearer {token})
    try:
        token = auth_header.split(' ')[1] if ' ' in auth_header else auth_header
        openid = verify_token(token)
        user = UserProfile.query.filter_by(openid=openid).first()
        return user
    except Exception as e:
        current_app.logger.warning(f'Token验证失败: {str(e)}')
        return None


@user_bp.route('/info', methods=['GET'])
def get_user_info():
    """
    获取当前用户信息
    
    请求头:
        Authorization: Bearer {token}
    
    响应:
        {
            "success": true,
            "data": {
                "openid": "用户openid",
                "nickName": "昵称",
                "avatarUrl": "头像URL",
                "joinDate": "2026-01-27",
                "agentCount": 24,
                "conversationCount": 1200
            }
        }
    """
    user = get_current_user()
    if not user:
        return error_response('未授权，请先登录', 401)
    
    return success_response(user.to_dict())


@user_bp.route('/info', methods=['PUT'])
def update_user_info():
    """
    更新当前用户信息
    
    请求头:
        Authorization: Bearer {token}
    
    请求体:
        {
            "nickName": "新昵称" (可选),
            "avatarUrl": "新头像URL" (可选)
        }
    
    响应:
        {
            "success": true,
            "data": {
                "openid": "用户openid",
                "nickName": "新昵称",
                "avatarUrl": "新头像URL",
                ...
            }
        }
    """
    user = get_current_user()
    if not user:
        return error_response('未授权，请先登录', 401)
    
    try:
        data = request.get_json()
        if not data:
            return error_response('请求体不能为空', 400)
        
        # 更新昵称
        if 'nickName' in data:
            user.nick_name = data['nickName']
        
        # 更新头像
        if 'avatarUrl' in data:
            user.avatar_url = data['avatarUrl']
        
        db.session.commit()
        current_app.logger.info(f'用户信息已更新: {user.openid}')
        
        return success_response(user.to_dict())
        
    except Exception as e:
        current_app.logger.error(f'更新用户信息失败: {str(e)}')
        db.session.rollback()
        return error_response('更新用户信息失败', 500)
