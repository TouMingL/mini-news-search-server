# app/middlewares/auth.py
# 认证中间件

from functools import wraps
from flask import request, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity, verify_jwt_in_request
from app.models import UserProfile
from app.utils.response import error_response


def get_current_user():
    """
    获取当前用户（供路由使用）
    
    注意：此函数需要在JWT验证通过后调用
    使用前需要先调用verify_jwt_in_request()或使用@jwt_required装饰器
    
    Returns:
        UserProfile: 用户对象，如果用户不存在返回None
    """
    try:
        # 验证JWT（如果还没有验证）
        verify_jwt_in_request(optional=True)
        openid = get_jwt_identity()
        
        if not openid:
            return None
        
        user = UserProfile.query.filter_by(openid=openid).first()
        return user
    except Exception as e:
        current_app.logger.warning(f'获取当前用户失败: {str(e)}')
        return None


def auth_required(f):
    """
    认证装饰器（增强版jwt_required）
    
    使用方式:
        @user_bp.route('/info')
        @auth_required
        def get_user_info(user):
            # user参数自动注入，无需手动获取
            return success_response(user.to_dict())
    
    Args:
        f: 被装饰的函数
        
    Returns:
        装饰后的函数，自动注入user参数
    """
    @wraps(f)
    @jwt_required()
    def decorated(*args, **kwargs):
        # 获取当前用户
        openid = get_jwt_identity()
        if not openid:
            return error_response('未授权，请先登录', 401)
        
        user = UserProfile.query.filter_by(openid=openid).first()
        if not user:
            current_app.logger.warning(f'用户不存在: openid={openid[:10]}...')
            return error_response('用户不存在', 401)
        
        # 将user作为参数注入到被装饰的函数
        return f(user=user, *args, **kwargs)
    
    return decorated


def optional_auth(f):
    """
    可选认证装饰器（JWT可选）
    
    使用场景：某些接口需要用户信息，但未登录时也可以访问（返回部分数据）
    
    使用方式:
        @user_bp.route('/public')
        @optional_auth
        def get_public_info(user=None):
            if user:
                # 已登录用户，返回完整数据
                return success_response(full_data)
            else:
                # 未登录用户，返回部分数据
                return success_response(partial_data)
    
    Args:
        f: 被装饰的函数
        
    Returns:
        装饰后的函数，user参数可能为None
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        user = None
        try:
            # 尝试验证JWT（可选）
            verify_jwt_in_request(optional=True)
            openid = get_jwt_identity()
            
            if openid:
                user = UserProfile.query.filter_by(openid=openid).first()
        except Exception as e:
            # JWT验证失败不影响，继续执行
            current_app.logger.debug(f'可选认证失败（正常）: {str(e)}')
        
        # 将user作为参数注入（可能为None）
        return f(user=user, *args, **kwargs)
    
    return decorated
