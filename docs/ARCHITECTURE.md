# 前后端对话流程架构文档

本文档描述 Unified Chat Gateway 重构后的前后端对话流程。

---

## 1. 总体架构

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              前端（微信小程序）                                    │
├─────────────────────────────────────────────────────────────────────────────────┤
│  pages/chat/chat.js                                                              │
│       │ onSendTap / onInputConfirm                                               │
│       ▼                                                                          │
│  chatService.sendMessage(chatId, content)                                        │
│       │                                                                          │
│       ▼                                                                          │
│  apiService.chat(content, chatId, { onChunk })                                   │
│       │  POST /api/chat  [唯一入口]                                               │
└───────┼─────────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              后端（Flask）                                        │
├─────────────────────────────────────────────────────────────────────────────────┤
│  app/api/chat.py  →  POST /api/chat                                              │
│       │                                                                          │
│       ▼                                                                          │
│  Pipeline.run_stream(PipelineInput)                                              │
│       │                                                                          │
│       ├── 1. 加载对话历史（DB）                                                   │
│       ├── 2. 时间解析（TemporalResolver + TimeIntentClassifier）                  │
│       ├── 3. 路由分类（RouteLLM）                                                 │
│       ├── 4. 追问类型/时间继承                                                    │
│       ├── 5. 查询改写（QueryRewriter）                                            │
│       ├── 6. 路由决策（Router.decide）                                            │
│       └── 7. 执行层                                                              │
│              ├── search_then_generate → 检索 + LLM 生成                           │
│              └── generate_direct     → LLM 直接生成（带历史）                      │
│       │                                                                          │
│       ▼                                                                          │
│  SSE 流式响应  text/event-stream                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 前端流程

### 2.1 触发入口

| 触发方式 | 位置 | 行为 |
|----------|------|------|
| 点击发送按钮 | `pages/chat/chat.js` `onSendTap()` | 调用 `sendMessage()` |
| 输入框回车 | `pages/chat/chat.js` `onInputConfirm()` | 调用 `sendMessage()` |

### 2.2 chatService.sendMessage(chatId, content)

**文件**: `miniprogram-1/utils/chatService.js`

**流程**:

1. **会话初始化**（如无则创建）
   - 检查 `storageService.getConversationsList()` 中是否存在该 chatId
   - 不存在则 `createConversation()`，使用 `app.globalData.selectedAgent` 作为 agent 快照

2. **标题更新**（首条消息）
   - 若标题为「未发送」，用消息摘要更新

3. **本地消息写入**
   - `addUserMessage(chatId, content)` 写入用户消息
   - `addAiReply(chatId, '思考中...')` 占位 AI 消息

4. **调用统一接口**
   - `apiService.chat(content, chatId, { onChunk })`
   - `onChunk`：每收到 SSE 内容块，更新 AI 消息 content，并 `refreshChatPage()` 刷新页面

5. **完成回调**
   - `updateAiMessage(result.answer)`：更新最终回复、生成预览、调用 `appendConversationMessages` 同步到服务器
   - `onAgentComplete()`：未读小红点、会话列表刷新

6. **错误处理**
   - 失败时 `updateAiMessage(defaultReply)`，默认文案为「原神牛逼」

### 2.3 apiService.chat(query, conversationId, options)

**文件**: `miniprogram-1/utils/apiService.js`

**请求**:
- URL: `POST {SERVER_BASE_URL}/api/chat`
- Header: `Authorization: Bearer {token}`（如有）
- Body: `{ query, conversation_id }`

**响应**: SSE 流 (enableChunked)
- 每行 `data: <json>\n\n`
- 解析 `parseRAGSSELine()`，处理：
  - `choices[0].delta.content`：追加到 accumulated，调用 `onChunk(accumulated)`
  - `replace`：整段替换 accumulated
  - `done: true`：记录 `sources`，结束

**返回**: `Promise<{ answer, sources }>`

---

## 3. 后端流程

### 3.1 统一入口 POST /api/chat

**文件**: `miniprogram-server/app/api/chat.py`

**职责**:
- 鉴权：`_get_openid_from_token()`
- 解析请求体：`query`（必填）、`conversation_id`、`history_turns`（默认 5）
- 构建 `PipelineInput`，调用 `Pipeline.run_stream()`
- 返回 SSE 流：`data: {json}\n\n` 逐行输出

### 3.2 Pipeline.run_stream()

**文件**: `miniprogram-server/app/services/pipeline.py`

**数据流**:

```
UserInput
    │
    ▼
┌───────────────────────────────────┐
│ 1. 加载对话历史                    │
│    _load_conversation_history()   │
│    从 DB ConversationMessage 查询  │
│    conversation_id + 最近 N 轮     │
└───────────────┬───────────────────┘
                ▼
┌───────────────────────────────────┐
│ 2. 时间解析                        │
│    TemporalResolver.resolve()     │
│    TimeIntentClassifier.classify()│
│    输出：temporal_context, time_intent │
└───────────────┬───────────────────┘
                ▼
┌───────────────────────────────────┐
│ 3. 分类层：RouteLLM                │
│    invoke(query, last_turn_category)│
│    本地小 LLM 做实体提取+意图识别  │
│    规则层 derive_route_output 推导 │
│    输出：RouteLLMOutput            │
│    (need_retrieval, need_scores,  │
│     filter_category, time_sensitivity, follow_up_time_type) │
└───────────────┬───────────────────┘
                ▼
┌───────────────────────────────────┐
│ 4. 追问类型/时间继承               │
│    追问时继承 history 推断的时间   │
│    输出：follow_up_type, answer_scope_date │
└───────────────┬───────────────────┘
                ▼
┌───────────────────────────────────┐
│ 5. 预处理层：QueryRewriter         │
│    rewrite(current_input, history,│
│     category_hint, follow_up_type)│
│    本地小 LLM 消解指代、补全省略   │
│    输出：standalone_query         │
└───────────────┬───────────────────┘
                ▼
┌───────────────────────────────────┐
│ 6. 决策层：Router.decide           │
│    decide(route_llm_output, state,│
│     standalone_query, temporal_   │
│     context, time_intent,         │
│     effective_last_category)      │
│    输出：RouteDecision.action     │
└───────────────┬───────────────────┘
                ▼
┌───────────────────────────────────┐
│ 7. 执行层：_execute_stream()       │
│    根据 action 分支执行            │
└───────────────────────────────────┘
                │
    ┌───────────┴───────────┐
    ▼                       ▼
search_then_generate    generate_direct
    │                       │
    │                       │ _build_chat_messages()
    │                       │ [system, history..., user]
    │                       │
    ▼                       ▼
LLM 改写检索词          LLM.chat_stream(messages)
VectorStore.search()
LLM.generate_answer_stream()
    │                       │
    └───────────┬───────────┘
                ▼
        yield SSE 事件
    │
    ▼
┌───────────────────────────────────┐
│ 8. 更新会话状态                    │
│    SessionStateManager.update_state│
│    last_category, search_count... │
└───────────────┬───────────────────┘
                ▼
┌───────────────────────────────────┐
│ 9. 记录日志                        │
│    PipelineLogger.log()           │
└───────────────────────────────────┘
```

### 3.3 路由决策规则 (Router.decide)

**文件**: `miniprogram-server/app/services/router.py`

**签名**:
```python
decide(
    route_llm_output: RouteLLMOutput,
    state: SessionState,
    standalone_query: str,
    temporal_context: Optional[TemporalContext] = None,
    time_intent: Optional[TimeIntent] = None,
    effective_last_category: Optional[str] = None,
) -> RouteDecision
```

**决策逻辑**（按优先级）:

| 条件 | action | 说明 |
|------|--------|------|
| 无意义查询保护 (need_retrieval 且查询无效) | generate_direct | 无法执行检索 |
| 搜索循环保护 (search_count >= 5) | generate_direct | 强制直接生成 |
| need_scores 且 category=sports | tool_scores | 赛况引擎 |
| need_retrieval == true | search_then_generate | 检索+生成 |
| 其他 | generate_direct | 直接生成 |

**上下文漂移**：当 `effective_last_category` 与 `route_llm_output.filter_category` 不同且非 general 时，重置 `state.search_count`，与删上数轮 Q&A 兼容。

**filter_category**：由 RouteLLM 输出（RouteLLMOutput.filter_category），与向量库 category 一致（academic/world/tech/economy/sports/general/health），无二次映射。Parser LLM 根据查询内容（如「羽毛球」-> sports）直接判断，避免语义 category 映射到 filter_category 时的信息损失。

### 3.4 执行层分支

**search_then_generate**:
1. `LLMService.rewrite_query_for_search(standalone_query)` 改写检索词
2. `VectorStore.search()` 向量检索（带 filter_category 等）
3. `LLMService.generate_answer_stream(query, context)` 基于检索结果流式生成
4. 校验失败时 yield `{"replace": "根据当前检索到的内容无法可靠回答..."}`
5. 结束 yield `{"sources": [...], "done": true}`

**generate_direct**:
1. `_build_chat_messages(current_query, history)` 组装 [system, ...history, user]
2. `LLMService.chat_stream(messages)` 流式生成
3. 结束 yield `{"sources": [], "done": true}`

---

## 4. 消息持久化与同步

### 4.1 前端本地存储

- **存储键**: `chat_messages_{chatId}`
- **写入时机**: `addUserMessage` / `addAiReply` 立即写入；`onChunk` 每次更新 AI 消息 content
- **完成时**: `updateAiMessage` 写入最终 content

### 4.2 后端同步

- **接口**: `POST /api/conversations/{chatId}/messages`
- **调用时机**: `updateAiMessage` 内，AI 回复完成后
- **请求体**: `userContent`, `agentContent`, `userMessageId`, `agentMessageId`, `title`, `agentSnapshot`（可选）
- **作用**: 将本回合 user + ai 消息追加到服务器，用于多端同步及 Pipeline 下次加载历史

### 4.3 Pipeline 历史加载

- **来源**: `ConversationMessage` 表，按 `conversation_id` 查询
- **顺序**: 按 `created_at` 倒序取最近 `history_turns * 2` 条，再反转成时间正序
- **格式**: `HistoryMessage(role, content, timestamp)`

---

## 5. SSE 事件格式

| 事件类型 | JSON 结构 | 说明 |
|----------|-----------|------|
| 内容块 | `{"choices":[{"delta":{"content":"..."}}]}` | 增量文本 |
| 替换事件 | `{"replace":"..."}` | 校验失败时整段替换 |
| 结束事件 | `{"sources":[...],"done":true}` | 流结束，sources 为检索来源（RAG 时） |

---

## 6. 相关接口一览

| 接口 | 用途 | 主流程是否调用 |
|------|------|----------------|
| POST /api/chat | 统一对话入口 | 是 |
| POST /api/conversations/{id}/messages | 消息落库同步 | 是（完成时） |
| POST /api/rag/query | RAG 查询（Pipeline） | 否（可选直连） |

---

## 7. 关键设计原则

- **单一入口**: 前端只调 `/api/chat`，不再根据意图选择不同接口
- **路由权在后端**: 意图判断、路由决策由 Pipeline 全权负责
- **无重复计算**: 每轮消息仅执行一次 rewrite + classify + route + execute
- **流式优先**: 全程 SSE 流式返回，前端实时展示
