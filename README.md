# Flask后端服务器

微信小程序后端API服务器，提供用户登录和用户信息管理功能。

## 环境要求

- Python 3.9
- Conda环境 py39

## 项目结构

```
miniprogram-server/
├── app/                    # 主应用包
│   ├── __init__.py        # 应用工厂
│   ├── config.py          # 配置（多环境支持）
│   ├── extensions.py      # 扩展管理
│   ├── models/            # 数据模型
│   ├── api/               # API蓝图
│   ├── utils/             # 工具函数
│   └── middlewares/       # 中间件
├── migrations/            # 数据库迁移
├── tests/                 # 单元测试
├── run.py                 # 启动入口
└── .env                   # 环境变量
```

## 安装依赖

```bash
# 激活conda环境
conda activate py39

# 安装依赖
pip install -r requirements.txt
```

## 配置环境变量

编辑 `.env` 文件，填入以下配置：

```
WECHAT_APPID=你的小程序AppID
WECHAT_SECRET=你的小程序Secret
JWT_SECRET_KEY=你的JWT密钥（建议使用随机字符串）
FLASK_ENV=development
DATABASE_URL=sqlite:///database.db
FLASK_HOST=0.0.0.0
FLASK_PORT=8081
```

## 启动服务器

### 方式1：使用run.py（推荐）

```bash
conda activate py39
python run.py
```

### 方式2：使用Flask CLI

```bash
conda activate py39
flask run
```

服务器将在 `http://0.0.0.0:8081` 启动。

## API接口

### 1. 微信登录

**POST** `/api/auth/login`

请求体:
```json
{
  "code": "微信登录凭证code"
}
```

响应:
```json
{
  "success": true,
  "data": {
    "token": "JWT token",
    "openid": "用户openid",
    "expiresIn": 604800
  }
}
```

### 2. 获取用户信息

**GET** `/api/user/info`

请求头:
```
Authorization: Bearer {token}
```

响应:
```json
{
  "success": true,
  "data": {
    "openid": "用户openid",
    "nickName": "昵称",
    "avatarUrl": "头像URL",
    "joinDate": "2026-01-27",
    "agentCount": 0,
    "conversationCount": 0
  }
}
```

### 3. 更新用户信息

**PUT** `/api/user/info`

请求头:
```
Authorization: Bearer {token}
```

请求体:
```json
{
  "nickName": "新昵称",
  "avatarUrl": "新头像URL"
}
```

## 测试接口

### 使用curl测试登录接口

```bash
curl -X POST http://localhost:8081/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"code":"test_code"}'
```

注意：需要使用真实的微信登录code才能成功。

### 使用curl测试获取用户信息

```bash
curl -X GET http://localhost:8081/api/user/info \
  -H "Authorization: Bearer {your_token}"
```

## 数据库

使用SQLite数据库，数据库文件 `database.db` 会在首次启动时自动创建。

## 架构说明

项目采用Flask Application Factory模式，支持多环境配置：
- **开发环境** (development): 默认环境，启用DEBUG
- **生产环境** (production): 通过 `FLASK_ENV=production` 设置
- **测试环境** (testing): 用于单元测试

## 注意事项

1. **环境要求**：必须在 `py39` conda环境中运行
2. **配置安全**：确保已配置微信小程序的AppID和Secret
3. **JWT密钥**：应该使用强随机字符串，不要使用默认值
4. **生产环境**：
   - 使用更安全的数据库（如MySQL或PostgreSQL）
   - 限制CORS来源为小程序域名
   - 设置 `FLASK_ENV=production`
   - 使用gunicorn等WSGI服务器（见部署文档）

## 迁移指南

如果从旧版本迁移，请参考 `docs/migration_guide.md`
