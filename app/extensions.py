# app/extensions.py
# 扩展实例化（统一管理，避免循环导入）

from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import JWTManager
from flask_migrate import Migrate

# 扩展实例（延迟初始化）
# 注意：这些实例在create_app()中通过init_app()方法初始化
db      = SQLAlchemy()
jwt     = JWTManager()
migrate = Migrate()
