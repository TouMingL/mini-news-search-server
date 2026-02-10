# app/utils/jwt_auth.py
# JWT认证工具

import jwt
from datetime import datetime, timedelta
from flask import current_app


def generate_token(openid):
    """
    生成JWT token
    
    Args:
        openid: 用户openid
        
    Returns:
        str: JWT token
    """
    secret_key = current_app.config['JWT_SECRET_KEY']
    algorithm = current_app.config['JWT_ALGORITHM']
    expiration = current_app.config['JWT_EXPIRATION_DELTA']
    
    payload = {
        'openid': openid,
        'exp': datetime.utcnow() + timedelta(seconds=expiration),
        'iat': datetime.utcnow()
    }
    
    token = jwt.encode(payload, secret_key, algorithm=algorithm)
    return token


def verify_token(token):
    """
    验证JWT token并返回openid
    
    Args:
        token: JWT token字符串
        
    Returns:
        str: 用户openid
        
    Raises:
        jwt.ExpiredSignatureError: token已过期
        jwt.InvalidTokenError: token无效
    """
    secret_key = current_app.config['JWT_SECRET_KEY']
    algorithm = current_app.config['JWT_ALGORITHM']
    
    try:
        payload = jwt.decode(token, secret_key, algorithms=[algorithm])
        return payload.get('openid')
    except jwt.ExpiredSignatureError:
        raise ValueError('Token已过期')
    except jwt.InvalidTokenError as e:
        raise ValueError(f'Token无效: {str(e)}')
