# Flask后端服务器

微信小程序后端API服务器。提供：用户登录与会话管理、统一对话入口（RAG + 直接生成）、时间解析与追问继承、赛况引擎等。

## 环境要求

- Python 3.9
- Conda环境 py39

## 项目结构

```
miniprogram-server/
├── app/
│   ├── __init__.py        # 应用工厂
│   ├── config.py          # 配置（多环境支持）
│   ├── extensions.py      # 扩展管理
│   ├── models/            # 数据模型
│   ├── api/               # API蓝图
│   │   ├── auth.py        # 微信登录
│   │   ├── user.py        # 用户信息
│   │   ├── chat.py        # 统一对话入口 POST /api/chat (SSE)
│   │   ├── conversations.py # 会话与消息同步
│   │   ├── rag.py         # RAG 相关
│   │   ├── sync.py        # 同步接口
│   │   └── notify.py      # 通知
│   ├── services/          # 核心业务服务
│   │   ├── pipeline.py    # 对话编排 Pipeline
│   │   ├── route_llm.py   # 路由分类 RouteLLM
│   │   ├── router.py      # 路由决策 Router
│   │   ├── query_rewriter.py # 查询改写
│   │   └── pipeline_modules/ # Pipeline 子模块
│   ├── utils/
│   └── middlewares/
├── docs/                  # 架构与设计文档
│   └── ARCHITECTURE.md    # 前后端对话流程
├── migrations/
├── tests/
├── run.py
└── .env
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

# NBA 数据源（按日期子目录组织）
NBA_BOXSCORE_ROOT=/path/to/parsed_boxscore
NBA_SCORES_ROOT=/path/to/sohu_nba_block4_scores
NBA_STANDINGS_ROOT=/path/to/sohu_nba_standings
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

### 0. 统一对话入口（主流程）

**POST** `/api/chat`

请求头:
```
Authorization: Bearer {token}  # 可选
```

请求体:
```json
{
  "query": "用户输入内容",
  "conversation_id": "会话ID",
  "history_turns": 5
}
```

响应: SSE 流 (`text/event-stream`)
- `choices[0].delta.content`: 增量文本
- `replace`: 校验失败时整段替换
- `sources`, `done`: 流结束

流程详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

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

- **对话 Pipeline**：`Pipeline.run_stream()` 串联「加载历史 → 时间解析 → RouteLLM 分类 → 追问/时间继承 → QueryRewriter 改写 → Router.decide → 执行层」。执行分支：`search_then_generate`（检索+生成）、`generate_direct`（直接生成）、`tool_scores`（赛况引擎）。
- **Flask 模式**：Application Factory，支持多环境配置：
  - **开发环境** (development): 默认环境，启用DEBUG
  - **生产环境** (production): 通过 `FLASK_ENV=production` 设置
  - **测试环境** (testing): 用于单元测试

详细数据流与 decide 签名见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

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
