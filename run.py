# run.py
# 应用启动入口

import os
from app import create_app

# 创建应用实例
app = create_app()

if __name__ == '__main__':
    # 从应用配置中获取服务器配置
    app.run(
        host=app.config['HOST'],
        port=app.config['PORT'],
        debug=app.config.get('DEBUG', False)
    )
