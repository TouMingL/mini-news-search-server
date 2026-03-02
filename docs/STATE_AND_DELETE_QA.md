# State 与「用户删上数轮 Q&A」的合理性及重构清单

## 原则

**判断依据**：在「用户可能删掉上数轮 Q&A」的前提下，state 的每个字段和依赖 state 的逻辑是否仍然**语义合理**——即是否表示「用户当前可见对话」的事实，而非「服务端历史上处理过的轮次」的汇总。

- 若某状态表示的是「服务端已处理轮次」的汇总，而用户删 Q&A 后不会回写服务端，则该状态与用户可见对话**可能不一致**，需标注、重构或移除。
- 不应以「是否有模块依赖该变量」决定保留与否，而应以「该状态在删 Q&A 场景下是否合理」为准。

---

## State 各字段语义与合理性

| 字段 | 当前语义 | 用户删上数轮 Q&A 后是否合理 | 结论 |
|------|----------|-----------------------------|------|
| **last_filter_category** | 上一轮（服务端处理的）分类 | 否。state 可能对应已删的那一轮，与用户可见「上一轮」不一致 | 已停用：last_turn_category 与漂移均从 history 推断；**应停止写入**，避免误导 |
| **search_count** | 服务端「连续检索」次数 | 部分合理。表示「本会话我们连续做了几次检索」，用于防服务端死循环；与用户可见轮数可能不一致（如用户只看到 2 轮，我们已做了 5 次检索） | **保留**，明确注释为「服务端连续检索计数，与用户可见轮次可能不一致」 |
| **last_route** | 上一轮路由动作 | 与用户可见可能不一致（同上） | 仅写未读，可保留作调试或后续移除 |
| **turn_count** | 服务端处理轮数 | 用于 clear_expired_states 驱逐顺序，不表示用户可见轮数 | 保留，注释为服务端计数 |
| **last_entities** | 上一轮实体 | 未写入，且与用户可见不一致 | 可移除或保留占位 |

---

## 需重构的模块

### 1. SessionState / SessionStateManager（已做 / 建议）

- **update_state 写入 last_filter_category**  
  - 问题：last_turn_category 与漂移已不读 state，继续写入会使 state 与用户可见不一致且易被误用。  
  - **重构**：不再写入 `last_filter_category`；在 state 注释中说明「上轮类别以 pipeline 从当前请求 history 推断为准」。
- **search_count / turn_count**  
  - **重构**：在字段注释或 update_state 注释中写明「服务端计数，用户删上数轮 Q&A 后与用户可见轮次可能不一致」。

### 2. SessionStateManager.should_inherit_category / detect_context_drift

- **问题**：二者读取 `state.last_filter_category`，语义为「上一轮类别」；用户删 Q&A 后 state 可能对应已删轮次，结果不合理。
- **重构**：改为以「上轮类别」入参为准（由 pipeline 从当前请求 history 推断的 effective_last_category），不再依赖 state：
  - 签名改为接收 `effective_last_category: Optional[str]`（可选保留 state 仅作 fallback 或移除对 state 的依赖）。
  - 若调用方未传 effective_last_category，则视为无上轮类别（与删 Q&A 后 history 变短一致）。

### 3. Router（搜索循环保护）

- **问题**：`is_search_loop(state)` 使用 `state.search_count`，为「服务端连续检索次数」。用户删上数轮后，该计数可能大于用户可见的连续检索次数，导致在用户看来「没问几次」就触发强制直接生成。
- **选项**：  
  - **A（当前建议）**：保持服务端防护语义，在 Router 与 SessionState 注释中明确「search_count 为服务端连续检索计数，与用户可见轮次可能不一致」。  
  - **B**：若产品要求「仅基于用户可见对话的连续检索次数」做保护，则需客户端或 history 提供每轮 action，再实现「基于当前 history 的连续检索次数」并传入 decide。
- **重构**：至少完成 A（注释）；若选 B 则需产品/客户端约定后再改。

### 4. Pipeline

- **已做**：last_turn_category 与漂移用 history 推断，不再依赖 state.last_filter_category；改写用 history 最后一轮用户输入作为上轮主题。  
- **无需再改**：get_state 仅用于向 Router 传 state（供 search_count）；last_filter_category 不再参与 pipeline 内逻辑。

### 5. 其他

- **last_route / last_entities**：无业务逻辑读取；可保留作调试或从 SessionState 中移除以简化语义。
- **route_and_update_state**：已增加参数 `effective_last_category` 并传入 `decide()`，调用方从 history 推断后传入即可与删 Q&A 兼容。

---

## 小结

| 模块 | 重构内容 | 状态 |
|------|----------|------|
| SessionState / update_state | 停写 last_filter_category；注释 search_count/turn_count 为服务端计数 | 已做 |
| should_inherit_category / detect_context_drift | 改为以 effective_last_category 入参为准，不读 state | 已做 |
| Router | 注释 search_count 为服务端计数、与用户可见可能不一致 | 已做 |
