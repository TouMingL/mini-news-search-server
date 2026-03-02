# Router.route_and_update_state() 与 effective_last_category

## 1. route_and_update_state() 在类中的位置

`Router`（`app/services/router.py`）中方法关系如下：

```
Router
├── __init__
├── state_manager (property)
├── decide(...)                    # 核心：根据 RouteLLM 输出 + state 做路由决策
├── _build_search_params(...)      # 内部：为 search_then_generate 构建检索参数
└── route_and_update_state(...)   # 便捷：get_state + decide + update_state 一步完成
```

- **decide()**：只做决策，不写 state。需要 `effective_last_category` 做「上下文漂移」和「搜索循环」相关逻辑。
- **route_and_update_state()**：先 `get_state(conversation_id)`，再调 **decide()**，再用 `classification_from_route_output` 调 **update_state()**，返回 `(decision, updated_state)`。  
  当前实现里，**decision 由内部再次调用 decide() 得到**，入参里传入的 `route_decision` 未被使用。

因此 `route_and_update_state()` 的语义是：「对某会话做一次路由决策并更新会话状态」；决策逻辑与单次调用 `decide()` 完全一致，所以也必须把 `effective_last_category` 传进这次内部的 `decide()` 调用。

---

## 2. 为何必须传 effective_last_category

### 2.1 在 decide() 里的两处使用

`effective_last_category` 在 `decide()` 中只用于一处逻辑（约 L144–149）：

```python
# 上下文漂移：当前类别与「当前请求 history 推断的上轮类别」不同且非 general 时重置连续搜索计数
if (effective_last_category is not None
    and route_llm_output.filter_category != effective_last_category
    and route_llm_output.filter_category != "general"):
    logger.info("检测到上下文漂移，重置连续搜索计数")
    state.search_count = 0
```

含义：

- **effective_last_category**：由调用方从**当前请求的 history** 推断出的「上一轮用户可见的类别」（例如 pipeline 里用 `_get_last_turn_category(history)`）。
- 若**当前轮** RouteLLM 判定的 `filter_category` 与这个「上轮类别」不同，且当前不是 `general`，则认为是**主题切换**（上下文漂移），把 **state.search_count 置 0**。
- 紧接着下面会用 **state.search_count** 做**搜索循环保护**：若 `search_count >= MAX_SEARCH_COUNT`，强制改为 `generate_direct`，避免同一主题下连续检索过多轮。

因此：

- **传了 effective_last_category**：漂移时重置 `search_count`，新主题可以重新计连续检索次数，循环保护语义正确。
- **没传（为 None）**：上面的 `if` 不成立，**永远不会因漂移重置 search_count**。

### 2.2 不传时会出现的问题

若通过 **route_and_update_state()** 调用（即内部调 decide 时未传 `effective_last_category`）：

1. **上下文漂移不重置计数**  
   用户先问多轮体育（sports），再切到经济（economy）。  
   - 正确行为：检测到 sports → economy，应把 `search_count` 置 0，经济类可以重新计 5 次检索。  
   - 不传时：`effective_last_category is None`，不会重置，`search_count` 仍是体育阶段累积的值，可能已经很大。

2. **搜索循环保护错位**  
   - 若前面已在体育下连续检索了 5 次，`search_count == 5`，下一轮用户切到经济再问需要检索的问题。  
   - 正确行为：漂移时先重置为 0，再按经济类重新计数。  
   - 不传时：不会重置，可能刚一进经济类就满足 `is_search_loop(state)`，被强制 `generate_direct`，用户会感觉「只问了一句就被打断」。

3. **与「用户删上数轮 Q&A」的约定不一致**  
   设计上要求：**上轮类别**以「当前请求的 history」推断为准，不依赖 state 里存的旧值（见 STATE_AND_DELETE_QA.md）。  
   Pipeline 主路径是：从 history 算 `last_turn_category`，再传 `effective_last_category=last_turn_category` 给 `decide()`。  
   **route_and_update_state()** 若不再传 `effective_last_category`，就等价于「上轮类别 = 无」，和主路径语义不一致，且在漂移与删 Q&A 场景下都会偏离预期。

---

## 3. 结论与修改

- **原因**：`route_and_update_state()` 内部会再调一次 `decide()`，而 `decide()` 依赖 `effective_last_category` 做「上下文漂移时重置 search_count」；不传则漂移与搜索循环保护都会错。
- **位置**：`route_and_update_state()` 是 Router 的便捷方法，位于 `decide()` 与 `_build_search_params()` 之后，语义是「一次路由决策 + 状态更新」。
- **修改**：为 **route_and_update_state()** 增加参数 **effective_last_category: Optional[str] = None**，并在其内部调用 **decide()** 时传入该参数，与 pipeline 主路径保持一致。

---

## 4. 系统性检查：遗漏还是刻意

### 4.1 当前调用关系

全仓库检索结果：

- **route_and_update_state()**：仅在 `router.py` 内**定义**，**无任何调用方**（Pipeline、API、测试均未调用）。
- **decide()**：仅由 **Pipeline** 调用（`run()` 与 `run_stream()` 各一处），且均传入 `effective_last_category=last_turn_category`。
- **update_state()**：仅由 **Pipeline** 在 decide 之后直接调用 `state_manager.update_state(...)`，不经过 Router。

即：主流程从未使用 `route_and_update_state()`，而是「get_state → decide → 执行 → update_state」分步在 Pipeline 内完成。

### 4.2 Pipeline 主流程（简化）

```
run() / run_stream() 内：
  1. state = self.state_manager.get_state(conversation_id)
  2. last_turn_category = _get_last_turn_category(history)   # 从当前请求 history 推断
  3. route_decision = self.router.decide(
       route_llm_output, state, standalone_query,
       temporal_context=..., time_intent=...,
       effective_last_category=last_turn_category,           # 显式传入
     )
  4. ... _execute(route_decision, ...) ...
  5. self.state_manager.update_state(conversation_id, classification, route_decision.action)
```

若用 `route_and_update_state()` 封装，等价于把步骤 1、3、5 合成一次调用；但 Pipeline 没有采用这种写法，而是手写 1→3→5，并在 3 中传入 `effective_last_category`。

### 4.3 推论：真实遗漏，非刻意保留

- **时间线/设计上**：先有「上轮类别从 history 推断、不依赖 state」的约定（STATE_AND_DELETE_QA），并给 **decide()** 增加了 `effective_last_category`。Pipeline 实现时直接调用 `decide(..., effective_last_category=last_turn_category)` 并单独 `update_state`，没有改用 `route_and_update_state()`。**route_and_update_state()** 从未被接到「带 history 的路径」上，也**没有**随之增加 `effective_last_category` 参数并传入 decide。

- **为何不是刻意**：若刻意认为「route_and_update_state 只用于无 history 的简化路径」，则不应在文档（STATE_AND_DELETE_QA）里写「若将来被调用，需传入 last_turn_category」——文档明确预期的是「被调用时要传」，而不是「此方法永不用于带 history 的场景」。方法语义是「一次路由决策 + 状态更新」，与 decide 的契约一致；若不支持传入 `effective_last_category`，则与 decide() 的公开契约不一致，属 API 不一致的遗漏。

- **结论**：这是**实现/演进过程中的遗漏**：主路径正确使用了 `effective_last_category`，但便捷方法 `route_and_update_state()` 未同步增加参数并传入 decide()，且当前无调用方，问题一直未被触发。补上参数并传入 decide() 后，将来若有调用方（例如其他 API 或脚本）使用该方法且具备 history，行为会与主路径一致，无需再改。

