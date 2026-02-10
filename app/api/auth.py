# app/api/auth.py
# 认证相关接口

from flask import Blueprint, request, current_app
from app.models import db, UserProfile
from app.utils.response import success_response, error_response
from app.utils.wechat import code2session
from app.utils.jwt_auth import generate_token
from app.utils.exceptions import (
    WeChatAPIError, WeChatConfigError, WeChatNetworkError, TokenGenerationError
)

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
    # 记录登录请求
    current_app.logger.info('收到登录请求')
    
    try:
        # 1. 参数验证
        data = request.get_json()
        if not data or 'code' not in data:
            current_app.logger.warning('登录请求缺少code参数')
            return error_response('缺少code参数', 400)
        
        code = data.get('code', '').strip()
        if not code:
            current_app.logger.warning('登录请求code为空')
            return error_response('code不能为空', 400)
        
        # 2. 调用微信接口获取openid和session_key
        try:
            current_app.logger.info(f'开始调用微信API，code: {code[:10]}...')
            wechat_data = code2session(code)
            openid = wechat_data.get('openid')
            unionid = wechat_data.get('unionid')
            session_key = wechat_data.get('session_key')
            
            if not openid:
                current_app.logger.error('微信API返回数据中缺少openid')
                return error_response('获取openid失败，请重试', 500)
            
            current_app.logger.info(f'微信API调用成功，openid: {openid[:10]}...')
            
        except WeChatConfigError as e:
            current_app.logger.error(f'微信配置错误: {str(e)}')
            return error_response('服务器配置错误，请联系管理员', 500)
            
        except WeChatAPIError as e:
            # 微信API业务错误（如code无效），返回友好提示
            current_app.logger.warning(f'微信API业务错误: {str(e)}')
            return error_response(str(e), 400)
            
        except WeChatNetworkError as e:
            # 网络错误，建议重试
            current_app.logger.error(f'微信API网络错误: {str(e)}')
            return error_response('网络连接失败，请稍后重试', 503)
            
        except Exception as e:
            current_app.logger.error(f'微信API调用未知错误: {str(e)}', exc_info=True)
            return error_response('登录失败，请稍后重试', 500)
        
        # 3. 查询或创建用户资料
        try:
            import time
            user = UserProfile.query.filter_by(openid=openid).first()
            is_new_user = False

            if not user:
                # 新用户，创建记录
                is_new_user = True
                user = UserProfile(
                    openid=openid,
                    nick_name='用户',
                    join_time=int(time.time() * 1000),
                )
                db.session.add(user)
                db.session.commit()
                current_app.logger.info(f'新用户注册成功: openid={openid[:10]}...')
            else:
                current_app.logger.info(f'用户登录: openid={openid[:10]}...')
                    
        except Exception as e:
            current_app.logger.error(f'数据库操作失败: {str(e)}', exc_info=True)
            db.session.rollback()
            return error_response('用户信息处理失败，请重试', 500)
        
        # 4. 生成JWT token
        try:
            token = generate_token(openid)
            expires_in = current_app.config['JWT_EXPIRATION_DELTA']
            
            current_app.logger.info(
                f'登录成功: openid={openid[:10]}..., is_new_user={is_new_user}, expires_in={expires_in}'
            )
            
            return success_response({
                'token': token,
                'openid': openid,
                'expiresIn': expires_in
            })
            
        except Exception as e:
            current_app.logger.error(f'生成token失败: {str(e)}', exc_info=True)
            return error_response('生成token失败，请重试', 500)
            
    except Exception as e:
        current_app.logger.error(f'登录接口未知异常: {str(e)}', exc_info=True)
        return error_response('服务器内部错误，请稍后重试', 500)
