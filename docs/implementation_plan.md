# 后端框架构建实施计划

## 实施路线图

```
阶段一：架构重构 (Week 1)
  ├── 1.1 创建Application Factory结构
  ├── 1.2 实现extensions.py
  ├── 1.3 重构config.py（多环境）
  └── 1.4 创建中间件层

阶段二：安全加固 (Week 1-2)
  ├── 2.1 升级JWT实现（Flask-JWT-Extended）
  ├── 2.2 实现token刷新和登出
  ├── 2.3 完善CORS配置
  ├── 2.4 添加输入验证（marshmallow）
  └── 2.5 实现请求限流

阶段三：数据库优化 (Week 2)
  ├── 3.1 集成Flask-Migrate
  ├── 3.2 配置数据库连接池
  └── 3.3 完善事务管理

阶段四：代码质量 (Week 2-3)
  ├── 4.1 统一异常处理
  ├── 4.2 配置日志系统
  └── 4.3 添加单元测试

阶段五：部署准备 (Week 3)
  ├── 5.1 配置gunicorn
  ├── 5.2 添加健康检查
  └── 5.3 创建部署文档
```

## 详细实施步骤

### 阶段一：架构重构

#### 步骤1.1：创建新的目录结构

**操作**：
1. 创建`app/`目录作为主应用包
2. 移动现有文件到新结构：
   - `models/` → `app/models/`
   - `api/` → `app/api/`
   - `utils/` → `app/utils/`
3. 创建新目录：
   - `app/middlewares/`
   - `migrations/`
   - `tests/`

**文件变更**：
```
miniprogram-server/
├── app/
│   ├── __init__.py          # 新建：应用工厂
│   ├── config.py            # 移动并重构
│   ├── extensions.py         # 新建：扩展管理
│   ├── models/              # 移动
│   ├── api/                 # 移动
│   ├── utils/               # 移动
│   └── middlewares/         # 新建
├── migrations/              # 新建
├── tests/                   # 新建
├── .flaskenv                # 新建
├── run.py                   # 新建：启动入口
└── app.py                   # 保留（向后兼容）或删除
```

#### 步骤1.2：实现app/extensions.py

**内容**：
```python
# app/extensions.py
from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import JWTManager
from flask_migrate import Migrate

# 扩展实例（延迟初始化）
db = SQLAlchemy()
jwt = JWTManager()
migrate = Migrate()
```

#### 步骤1.3：重构app/config.py

**内容**：
```python
# app/config.py
import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    """基础配置"""
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key')
    
    # 数据库
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL', 'sqlite:///database.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_size': 10,
        'pool_recycle': 3600,
        'pool_pre_ping': True
    }
    
    # 微信
    WECHAT_APPID = os.getenv('WECHAT_APPID', '')
    WECHAT_SECRET = os.getenv('WECHAT_SECRET', '')
    
    # JWT（Flask-JWT-Extended配置）
    JWT_SECRET_KEY = os.getenv('JWT_SECRET_KEY', 'jwt-secret-key')
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=30)
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=7)
    JWT_TOKEN_LOCATION = ['headers']
    JWT_HEADER_NAME = 'Authorization'
    JWT_HEADER_TYPE = 'Bearer'
    
    # CORS
    CORS_ORIGINS = os.getenv('CORS_ORIGINS', '*').split(',')
    
    # 限流
    RATELIMIT_STORAGE_URL = os.getenv('REDIS_URL', 'memory://')
    
class DevelopmentConfig(Config):
    """开发环境配置"""
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL', 'sqlite:///database.db')

class ProductionConfig(Config):
    """生产环境配置"""
    DEBUG = False
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL')
    CORS_ORIGINS = os.getenv('CORS_ORIGINS', '').split(',')
    
    # 生产环境必须配置
    if not SQLALCHEMY_DATABASE_URI:
        raise ValueError('生产环境必须配置DATABASE_URL')

class TestingConfig(Config):
    """测试环境配置"""
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'

config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}
```

#### 步骤1.4：实现app/__init__.py（应用工厂）

**内容**：
```python
# app/__init__.py
from flask import Flask
from flask_cors import CORS
from .config import config
from .extensions import db, jwt, migrate

def create_app(config_name=None):
    """应用工厂函数"""
    app = Flask(__name__)
    
    # 加载配置
    config_name = config_name or os.getenv('FLASK_ENV', 'development')
    app.config.from_object(config[config_name])
    
    # 初始化扩展
    db.init_app(app)
    jwt.init_app(app)
    migrate.init_app(app, db)
    
    # 配置CORS
    CORS(app, 
         origins=app.config['CORS_ORIGINS'],
         supports_credentials=True)
    
    # 注册蓝图
    from .api.auth import auth_bp
    from .api.user import user_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(user_bp)
    
    # 注册中间件
    from .middlewares.error_handler import register_error_handlers
    register_error_handlers(app)
    
    # 初始化数据库（仅开发环境）
    if app.config['DEBUG']:
        with app.app_context():
            db.create_all()
    
    return app
```

#### 步骤1.5：创建中间件层

**app/middlewares/auth.py**：
```python
# app/middlewares/auth.py
from functools import wraps
from flask import request
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.models import User
from app.utils.response import error_response

def get_current_user():
    """获取当前用户（供路由使用）"""
    from flask_jwt_extended import get_jwt_identity
    openid = get_jwt_identity()
    if not openid:
        return None
    return User.query.filter_by(openid=openid).first()

def auth_required(f):
    """认证装饰器（增强版jwt_required）"""
    @wraps(f)
    @jwt_required()
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return error_response('用户不存在', 401)
        return f(user=user, *args, **kwargs)
    return decorated
```

**app/middlewares/error_handler.py**：
```python
# app/middlewares/error_handler.py
from flask import jsonify
from flask_jwt_extended.exceptions import JWTExtendedException
from app.utils.response import error_response

def register_error_handlers(app):
    """注册全局错误处理器"""
    
    @app.errorhandler(404)
    def not_found(error):
        return error_response('接口不存在', 404)
    
    @app.errorhandler(500)
    def internal_error(error):
        app.logger.error(f'服务器内部错误: {str(error)}')
        return error_response('服务器内部错误', 500)
    
    @app.errorhandler(JWTExtendedException)
    def jwt_error(error):
        return error_response('Token无效或已过期', 401)
    
    @app.errorhandler(ValueError)
    def value_error(error):
        return error_response(str(error), 400)
```

### 阶段二：安全加固

#### 步骤2.1：升级JWT实现

**更新requirements.txt**：
```txt
Flask==2.3.3
Flask-SQLAlchemy==3.0.5
Flask-JWT-Extended==4.5.3      # 新增
Flask-Migrate==4.0.5
Flask-CORS==4.0.0
Flask-Limiter==3.5.0           # 新增
marshmallow==3.20.1            # 新增
python-dotenv==1.0.0
requests==2.31.0
redis==5.0.1                    # 可选：token黑名单
```

**重构app/utils/jwt_auth.py**：
```python
# app/utils/jwt_auth.py
from flask_jwt_extended import create_access_token, create_refresh_token
from datetime import timedelta

def generate_tokens(openid):
    """生成access token和refresh token"""
    access_token = create_access_token(
        identity=openid,
        additional_claims={'openid': openid}
    )
    refresh_token = create_refresh_token(identity=openid)
    return access_token, refresh_token
```

**更新app/api/auth.py**：
```python
# app/api/auth.py
from flask import Blueprint, request
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from app.models import db, User
from app.utils.response import success_response, error_response
from app.utils.wechat import code2session
from app.utils.jwt_auth import generate_tokens
from app.middlewares.auth import auth_required

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')

@auth_bp.route('/login', methods=['POST'])
def login():
    """微信登录"""
    # ... 现有逻辑 ...
    access_token, refresh_token = generate_tokens(openid)
    return success_response({
        'access_token': access_token,
        'refresh_token': refresh_token,
        'openid': openid
    })

@auth_bp.route('/refresh', methods=['POST'])
@jwt_required(refresh=True)
def refresh():
    """刷新access token"""
    current_openid = get_jwt_identity()
    access_token = create_access_token(identity=current_openid)
    return success_response({
        'access_token': access_token
    })

@auth_bp.route('/logout', methods=['POST'])
@jwt_required()
def logout():
    """登出（将token加入黑名单）"""
    jti = get_jwt()['jti']
    # 将jti加入Redis黑名单（需要实现）
    # redis_client.setex(f'blacklist:{jti}', 3600, 'true')
    return success_response({'message': '登出成功'})
```

#### 步骤2.2：添加输入验证

**创建app/schemas/auth_schema.py**：
```python
# app/schemas/auth_schema.py
from marshmallow import Schema, fields, validate

class LoginSchema(Schema):
    code = fields.Str(required=True, validate=validate.Length(min=1))
```

**更新app/api/auth.py**：
```python
from app.schemas.auth_schema import LoginSchema

@auth_bp.route('/login', methods=['POST'])
def login():
    schema = LoginSchema()
    try:
        data = schema.load(request.get_json())
    except ValidationError as err:
        return error_response('参数验证失败', 400, err.messages)
    # ... 后续逻辑 ...
```

#### 步骤2.3：实现请求限流

**app/__init__.py中添加**：
```python
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"]
)

# 在登录接口上应用限流
@auth_bp.route('/login', methods=['POST'])
@limiter.limit("5 per minute")
def login():
    # ...
```

### 阶段三：数据库优化

#### 步骤3.1：集成Flask-Migrate

**初始化迁移**：
```bash
flask db init
flask db migrate -m "Initial migration"
flask db upgrade
```

**移除app.py中的db.create_all()**

#### 步骤3.2：配置数据库连接池

已在config.py中配置（见步骤1.3）

### 阶段四：代码质量

#### 步骤4.1：统一异常处理

已在步骤1.5中实现error_handler.py

#### 步骤4.2：配置日志系统

**app/__init__.py中添加**：
```python
import logging
from logging.handlers import RotatingFileHandler

def create_app(config_name=None):
    # ... 现有代码 ...
    
    # 配置日志
    if not app.debug:
        file_handler = RotatingFileHandler(
            'logs/app.log', maxBytes=10240000, backupCount=10
        )
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
        ))
        file_handler.setLevel(logging.INFO)
        app.logger.addHandler(file_handler)
        app.logger.setLevel(logging.INFO)
    
    return app
```

#### 步骤4.3：添加单元测试

**tests/test_auth.py**：
```python
# tests/test_auth.py
import pytest
from app import create_app, db
from app.models import User

@pytest.fixture
def app():
    app = create_app('testing')
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()

@pytest.fixture
def client(app):
    return app.test_client()

def test_login_success(client):
    response = client.post('/api/auth/login', json={'code': 'test_code'})
    assert response.status_code == 200
    assert 'access_token' in response.json['data']
```

### 阶段五：部署准备

#### 步骤5.1：配置gunicorn

**gunicorn_config.py**：
```python
# gunicorn_config.py
bind = "0.0.0.0:8081"
workers = 4
worker_class = "sync"
timeout = 120
keepalive = 5
```

**启动命令**：
```bash
gunicorn -c gunicorn_config.py "app:create_app()"
```

#### 步骤5.2：添加健康检查

**app/api/health.py**：
```python
# app/api/health.py
from flask import Blueprint
from app.models import db
from app.utils.response import success_response, error_response

health_bp = Blueprint('health', __name__)

@health_bp.route('/health', methods=['GET'])
def health_check():
    """健康检查接口"""
    try:
        # 检查数据库连接
        db.session.execute('SELECT 1')
        return success_response({'status': 'healthy'})
    except Exception as e:
        return error_response(f'服务异常: {str(e)}', 503)
```

## 迁移检查清单

### 代码迁移
- [ ] 创建新的目录结构
- [ ] 移动现有文件到app/目录
- [ ] 更新所有import路径
- [ ] 实现extensions.py
- [ ] 重构config.py（多环境）
- [ ] 实现应用工厂create_app()
- [ ] 创建中间件层
- [ ] 更新所有蓝图注册

### JWT升级
- [ ] 安装Flask-JWT-Extended
- [ ] 更新jwt_auth.py
- [ ] 更新auth.py（使用新JWT）
- [ ] 实现refresh接口
- [ ] 实现logout接口
- [ ] 更新所有使用JWT的路由

### 数据库迁移
- [ ] 安装Flask-Migrate
- [ ] 初始化迁移仓库
- [ ] 创建初始迁移
- [ ] 移除db.create_all()
- [ ] 测试迁移功能

### 安全加固
- [ ] 配置CORS（环境区分）
- [ ] 添加输入验证（marshmallow）
- [ ] 实现请求限流
- [ ] 配置token黑名单（可选）

### 代码质量
- [ ] 实现统一异常处理
- [ ] 配置日志系统
- [ ] 编写单元测试
- [ ] 配置测试覆盖率

### 部署准备
- [ ] 配置gunicorn
- [ ] 添加健康检查接口
- [ ] 创建部署文档
- [ ] 配置环境变量文档

## 测试验证

### 功能测试
1. 登录接口测试
2. Token刷新测试
3. 登出接口测试
4. 用户信息接口测试
5. 认证保护测试

### 性能测试
1. 并发请求测试
2. 数据库连接池测试
3. 限流功能测试

### 安全测试
1. Token过期测试
2. 无效Token测试
3. CORS跨域测试
4. 输入验证测试

## 回滚方案

如果迁移过程中出现问题：

1. **代码回滚**：使用Git回滚到迁移前版本
2. **数据库回滚**：使用`flask db downgrade`回滚迁移
3. **配置回滚**：恢复旧的config.py和app.py

## 注意事项

1. **备份数据**：迁移前备份数据库
2. **分步实施**：不要一次性完成所有改动，分阶段进行
3. **充分测试**：每个阶段完成后进行充分测试
4. **文档更新**：及时更新README和API文档
5. **团队沟通**：确保团队成员了解新的项目结构
