# api/auth.py
# 认证相关接口（与 app.api.auth 逻辑一致，使用 UserProfile）

import time
from flask import Blueprint, request, current_app
from app.models import db, UserProfile
from app.utils.response import success_response, error_response
from app.utils.wechat import code2session
from app.utils.jwt_auth import generate_token

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')


@auth_bp.route('/login', methods=['POST'])
def login():
    """
    微信登录接口
    
    请求体:
        {
            "code": "微信登录凭证code"
        }
    
    响应:
        {
            "success": true,
            "data": {
                "token": "JWT token",
                "openid": "用户openid",
                "expiresIn": 604800
            }
        }
    """
    try:
        data = request.get_json()
        if not data or 'code' not in data:
            return error_response('缺少code参数', 400)
        
        code = data.get('code')
        if not code:
            return error_response('code不能为空', 400)
        
        # 调用微信接口获取openid和session_key
        try:
            wechat_data = code2session(code)
            openid = wechat_data.get('openid')
            session_key = wechat_data.get('session_key')
            
            if not openid:
                return error_response('获取openid失败', 500)
        except Exception as e:
            current_app.logger.error(f'微信登录失败: {str(e)}')
            return error_response(f'登录失败: {str(e)}', 500)
        
        # 查询或创建用户资料（user_profile 表）
        user = UserProfile.query.filter_by(openid=openid).first()
        if not user:
            user = UserProfile(
                openid=openid,
                nick_name='用户',
                join_time=int(time.time() * 1000),
            )
            db.session.add(user)
            db.session.commit()
            current_app.logger.info(f'新用户注册: {openid}')
        
        # 生成JWT token
        try:
            token = generate_token(openid)
            expires_in = current_app.config['JWT_EXPIRATION_DELTA']
            
            return success_response({
                'token': token,
                'openid': openid,
                'expiresIn': expires_in
            })
        except Exception as e:
            current_app.logger.error(f'生成token失败: {str(e)}')
            return error_response('生成token失败', 500)
            
    except Exception as e:
        current_app.logger.error(f'登录接口异常: {str(e)}')
        return error_response('服务器内部错误', 500)
