# app/config.py
# 配置文件（多环境支持）

import os
from datetime import timedelta
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()


class Config:
    """基础配置类"""
    
    # Flask配置
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
    
    # 数据库配置
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL', 'sqlite:///database.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # 数据库连接池配置（生产环境使用MySQL/PostgreSQL时启用）
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_size': 10,
        'pool_recycle': 3600,
        'pool_pre_ping': True
    }
    
    # 微信小程序配置
    WECHAT_APPID = os.getenv('WECHAT_APPID', '')
    WECHAT_SECRET = os.getenv('WECHAT_SECRET', '')
    
    # JWT配置
    # 注意：当前仍使用原生PyJWT，但已添加Flask-JWT-Extended配置为后续迁移做准备
    JWT_SECRET_KEY = os.getenv('JWT_SECRET_KEY', 'jwt-secret-key-change-in-production')
    JWT_ALGORITHM = 'HS256'
    JWT_EXPIRATION_DELTA = 7 * 24 * 60 * 60  # 7天（秒）- 当前PyJWT使用
    
    # Flask-JWT-Extended配置（为后续迁移准备）
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=30)  # access token 30分钟
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=7)     # refresh token 7天
    JWT_TOKEN_LOCATION = ['headers']                   # 从请求头读取
    JWT_HEADER_NAME = 'Authorization'                  # 请求头名称
    JWT_HEADER_TYPE = 'Bearer'                         # Token类型
    
    # CORS配置
    CORS_ORIGINS = os.getenv('CORS_ORIGINS', '*').split(',') if os.getenv('CORS_ORIGINS') else ['*']
    
    # 服务器配置
    HOST = os.getenv('FLASK_HOST', '0.0.0.0')
    PORT = int(os.getenv('FLASK_PORT', 8081))
    
    # Qdrant向量数据库配置
    QDRANT_HOST = os.getenv('QDRANT_HOST', 'localhost')
    QDRANT_PORT = int(os.getenv('QDRANT_PORT', 6333))
    QDRANT_COLLECTION_NAME = os.getenv('QDRANT_COLLECTION_NAME', 'news_collection')
    
    # Embedding 独立服务（向量化模型只在该进程中加载一次）
    EMBEDDING_SERVICE_URL = os.getenv('EMBEDDING_SERVICE_URL', 'http://127.0.0.1:8083')
    EMBEDDING_SERVICE_TIMEOUT = int(os.getenv('EMBEDDING_SERVICE_TIMEOUT', 30))
    
    # GLM-4 API配置（用于RAG回答生成）
    GLM_API_KEY = os.getenv('GLM_API_KEY', '')
    GLM_API_BASE = os.getenv('GLM_API_BASE', 'https://open.bigmodel.cn/api/paas/v4')
    GLM_MODEL = os.getenv('GLM_MODEL', 'glm-4-flash')
    
    # 本地LLM服务配置（WSL vLLM OpenAI 兼容 API）
    # 用于 QueryRewriter 和 IntentClassifier 等低延迟任务
    LOCAL_LLM_API_BASE = os.getenv('LOCAL_LLM_API_BASE', 'http://localhost:8001/v1')
    LOCAL_LLM_API_KEY = os.getenv('LOCAL_LLM_API_KEY', '')  # vLLM 可选
    LOCAL_LLM_MODEL = os.getenv('LOCAL_LLM_MODEL', 'qwen2.5-3b-instruct-awq')
    LOCAL_LLM_TIMEOUT = int(os.getenv('LOCAL_LLM_TIMEOUT', 30))
    
    # Pipeline配置
    PIPELINE_LOG_DIR = os.getenv('PIPELINE_LOG_DIR', 'logs/pipeline')
    PIPELINE_MAX_HISTORY_TURNS = int(os.getenv('PIPELINE_MAX_HISTORY_TURNS', 5))


class DevelopmentConfig(Config):
    """开发环境配置"""
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL', 'sqlite:///database.db')


class ProductionConfig(Config):
    """生产环境配置"""
    DEBUG = False
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL')
    
    # 生产环境必须配置数据库
    if not SQLALCHEMY_DATABASE_URI:
        raise ValueError('生产环境必须配置DATABASE_URL')
    
    # 生产环境CORS应限制为小程序域名
    cors_origins = os.getenv('CORS_ORIGINS', '')
    if cors_origins:
        CORS_ORIGINS = cors_origins.split(',')
    else:
        # 如果没有配置，使用默认值（但应该配置）
        CORS_ORIGINS = ['*']


class TestingConfig(Config):
    """测试环境配置"""
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'


# 配置字典
config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}
