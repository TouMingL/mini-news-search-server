# app/middlewares/error_handler.py
# 全局错误处理器

from app.utils.response import error_response


def register_error_handlers(app):
    """注册全局错误处理器"""
    
    @app.errorhandler(404)
    def not_found(error):
        return error_response('接口不存在', 404)
    
    @app.errorhandler(500)
    def internal_error(error):
        app.logger.error(f'服务器内部错误: {str(error)}', exc_info=True)
        return error_response('服务器内部错误', 500)
    
    @app.errorhandler(400)
    def bad_request(error):
        return error_response('请求参数错误', 400)
    
    @app.errorhandler(401)
    def unauthorized(error):
        return error_response('未授权，请先登录', 401)
    
    @app.errorhandler(403)
    def forbidden(error):
        return error_response('权限不足', 403)
    
    @app.errorhandler(ValueError)
    def value_error(error):
        return error_response(str(error), 400)
    
    # Flask-JWT-Extended错误处理（为后续迁移准备）
    try:
        from flask_jwt_extended.exceptions import JWTExtendedException
        
        @app.errorhandler(JWTExtendedException)
        def jwt_error(error):
            error_messages = {
                'MissingAuthorizationHeader': '缺少Authorization请求头',
                'InvalidHeaderFormat': 'Authorization请求头格式错误',
                'TokenExpired': 'Token已过期，请重新登录',
                'InvalidTokenError': 'Token无效',
            }
            message = error_messages.get(error.__class__.__name__, 'Token验证失败')
            app.logger.warning(f'JWT错误: {str(error)}')
            return error_response(message, 401)
    except ImportError:
        # Flask-JWT-Extended未安装时跳过
        pass