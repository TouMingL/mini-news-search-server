# app/__init__.py
# Flask应用工厂

import os
from flask import Flask
from flask_cors import CORS
from app.config import config
from app.extensions import db, jwt, migrate
from app.middlewares.error_handler import register_error_handlers


def create_app(config_name=None):
    """
    应用工厂函数
    
    Args:
        config_name: 配置环境名称 ('development', 'production', 'testing')
                     如果为None，则从环境变量FLASK_ENV读取
    
    Returns:
        Flask: Flask应用实例
    """
    app = Flask(__name__)
    
    # 加载配置
    if config_name is None:
        config_name = os.getenv('FLASK_ENV', 'development')
    app.config.from_object(config[config_name])
    
    # 初始化扩展（注意顺序：db -> jwt -> migrate）
    db.init_app(app)
    jwt.init_app(app)
    migrate.init_app(app, db)
    
    # 配置CORS（允许小程序跨域请求）
    CORS(app, resources={
        r"/api/*": {
            "origins": app.config['CORS_ORIGINS'],
            "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            "allow_headers": ["Content-Type", "Authorization"],
            "supports_credentials": True
        }
    })
    
    # 注册蓝图
    from app.api.auth import auth_bp
    from app.api.user import user_bp
    from app.api.sync import sync_bp
    from app.api.conversations import conversations_bp
    from app.api.rag import rag_bp
    from app.api.chat import chat_bp
    from app.api.notify import notify_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(user_bp)
    app.register_blueprint(sync_bp)
    app.register_blueprint(conversations_bp)
    app.register_blueprint(rag_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(notify_bp)
    
    # 注册错误处理器
    register_error_handlers(app)
    
    # 创建数据库表（仅开发环境，生产环境应使用迁移）
    if app.config.get('DEBUG'):
        with app.app_context():
            db.create_all()
            app.logger.info('数据库表已创建（开发模式）')

    # Embedding 已剥离为独立服务（embedding_server.py），仅初始化一次，主服务通过 HTTP 调用
    return app
