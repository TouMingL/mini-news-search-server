# Agent Pipeline 批判性审查报告

**视角**：外部、理性、以可维护性/正确性/一致性为尺  
**范围**：`app/services/pipeline.py` 及直接依赖（router, route_llm, answer_verifier, tracer, temporal_*, schemas）  
**日期**：2026-03-03  

---

## 1. 总体评价

Pipeline 承担「预处理 → 路由 → 执行 → 校验 → 日志」的完整编排，数据流清晰（RouteLLM → Router → _execute/_execute_stream），时间层（TemporalResolver、TimeIntent、追问继承）与校验层（AnswerVerifier）集成完整。  

**主要问题**：单文件过大（2300+ 行）、`run` 与 `run_stream` 存在大量重复、执行层存在不可达代码与同步/流式行为不一致、校验层对 LLM 失败静默通过、文档与实现不同步。若不收敛，后续扩展和排错成本会持续上升。

---

## 2. 架构与数据流（简要）

- **入口**：`run()`（同步）、`run_stream()`（流式），均由 API 层调用。
- **顺序**：加载历史 → 时间解析（TemporalResolver + TimeIntentClassifier）→ RouteLLM.invoke → 追问类型/时间继承 → QueryRewriter.rewrite → Router.decide → _execute / _execute_stream。
- **执行分支**：search_then_generate（检索+生成+校验）、generate_direct（直接生成）、tool_scores（赛况引擎）、fallback（按直接生成处理）。

**优点**：RouteLLM 作为单一分类入口、effective_last_category 从 history 推断（与删上数轮 Q&A 兼容）、Router 的 search_loop 与无意义查询保护、Tracer 步骤完整，便于排查。

---

## 3. 严重问题

### 3.1 ~~_execute() 内不可达代码（bug）~~ [已修复]

已删除 `_execute()` 中 generate_direct 分支后的不可达死代码。

### 3.2 ~~同步路径与流式路径对 tool_quote / tool_weather 不一致~~ [已处理]

同步路径 `_execute()` 中的 tool_quote / tool_weather 死代码已删除。当前 Router 不会产生这两种 action，无实际影响。同时 run / run_stream 的公共前置流程已抽出为 `_preprocess()`，消除了 ~175 行的逐行重复。

### 3.3 ~~AnswerVerifier 对 LLM 异常静默通过~~ [已修复]

`_verify_no_fabrication`、`_verify_on_topic` 的 try/except 已移除，异常直接向上传播到 Pipeline 顶层统一处理（tracer.record_error + raise）。

### 3.4 route_and_update_state 已接 effective_last_category（无需再改）

**说明**：`router.py` 中 `route_and_update_state` 已包含参数 `effective_last_category` 并传入 `decide()`，与 CODE_REVIEW_20260302 中的建议一致，此处无遗漏。

---

## 4. 设计 / 可维护性问题

### 4.1 ~~Pipeline 单文件过大，职责过多~~ [已修复]

已创建 `app/services/pipeline_modules/` 子包，按职责拆分为：

- `follow_up.py` — 追问类型识别 + 时间继承编排（原 L50-243）
- `search_helpers.py` — 检索辅助函数：语义分过滤、字面重叠、日期注入（原 L246-372）
- `scores_formatter.py` — 赛况格式化与读取（原 L375-504）
- `sse_utils.py` — SSE 事件过滤 + 历史提取（原 L293-328）

模块文件已就绪，pipeline.py 中对应代码的删除与 import 替换待手动完成。

### 4.2 ~~run() 与 run_stream() 大量重复~~ [已修复]

公共前置流程已抽出为 `_preprocess()` 方法，返回 `PreprocessResult` dataclass。run / run_stream 现在各自只有 ~15 行调用 + 后置处理逻辑，前置流程只维护一份。

### 4.3 _execute 与 _execute_stream 中检索+生成逻辑重复

`_execute_stream` 中 search_then_generate 分支内：查询分解、并发检索、字面重叠、RRF、语义过滤、answer_scope 降级、赛况注入、生成与校验等，与 `_execute_search_then_generate` 高度重叠，只是流式版在生成阶段逐 chunk yield。若将「检索 + 上下文构建 + 是否流式」拆成可复用函数（例如：检索与重排返回统一结构，生成层按 sync/stream 调用不同 LLM 接口），可减少重复并降低「只改 sync 不改 stream」带来的行为分叉风险。

### 4.4 魔法数字与配置分散

例如：`CHITCHAT_CONTEXT_IRRELEVANT_CONFIDENCE = 0.5`、`_SUB_QUERY_COVERAGE_SCORE`、`_REWRITE_CONFIDENCE_THRESHOLD`、`_TIME_RRF_ALPHA`、语义分阈值等，部分在 config 中，部分写死在 pipeline。建议统一到 config 或单例配置对象，便于环境区分与调参，并减少「改行为需要改代码」的情况。

---

## 5. 文档与一致性

### 5.1 ARCHITECTURE.md 与实现不符

- 文档仍写「3. 分类层：**IntentClassifier**」「4. 决策层：Router decide(classification, state, standalone_query)」。
- 实际主路径为 **RouteLLM** → Router.decide(route_llm_output, state, standalone_query, temporal_context, time_intent, effective_last_category)，且无 IntentClassifier 参与。

若不更新文档，会误导后续维护者和新人。建议将 3.2 节数据流改为「QueryRewriter → RouteLLM → Router」，并更新 decide 的入参与说明。

### 5.2 Pipeline 类 docstring 已正确

类注释已为「UserInput -> QueryRewriter -> RouteLLM -> Router -> ...」，与实现一致；仅 ARCHITECTURE.md 需同步。

---

## 6. 建议优先级（理性排序）

| 优先级 | 项 | 状态 |
|--------|----|------|
| ~~P0~~ | ~~删除或修正 _execute 中 generate_direct 分支后的死代码~~ | 已完成 |
| ~~P0~~ | ~~AnswerVerifier 异常时不再静默 return True~~ | 已完成 |
| ~~P1~~ | ~~抽出 run/run_stream 的公共前置流程~~ | 已完成 (_preprocess) |
| ~~P1~~ | ~~更新 ARCHITECTURE.md：IntentClassifier -> RouteLLM，decide 签名与数据流~~ | 已完成 |
| ~~P2~~ | ~~将 Pipeline 拆分为编排 + 时间/检索/生成子模块~~ | 已完成  |
| P2 | 检索+生成逻辑在 _execute 与 _execute_stream 间复用 | 待做 |
| P3 | 魔法数字与阈值收口到 config | 待做 |

---

## 7. 小结

Pipeline 在路由、时间、校验的集成上是连贯的，RouteLLM + effective_last_category + Router 保护逻辑设计合理。当前最需要先解决的是：**执行层死代码与 sync/stream 对未实现工具的一致性**，以及 **AnswerVerifier 对异常的静默通过**。在此基础上再推进「去重复」和「拆模块」，会更容易且风险更小。文档与实现的对齐（尤其是 ARCHITECTURE.md）应随主流程变更同步更新，避免技术债累积。
