# Code Review: miniprogram-server (Post–RouteLLM / Answer Verification)

**Scope:** Uncommitted and recent changes under `app/services/`, `app/config.py`, `app/api/notify.py`, `embedding_server.py`, `requirements.txt`, `.gitignore`, and new modules (route_llm, temporal_*, answer_verifier, tools).

**Convention:** Commit messages must be in **pure English** (Conventional Commits style).

---

## 1. Architecture & Data Flow

- **RouteLLM as primary classifier:** Pipeline uses `get_route_llm().invoke(current_query, last_turn_category)` for routing; `IntentClassifier` remains in the pipeline constructor but is **not used** in the main sync/stream paths. Docstring at pipeline ~L404 still says "IntentClassifier -> Router"; consider updating to "RouteLLM -> Router" and optionally deprecate or remove unused intent_classifier wiring.
- **Temporal flow:** `TemporalResolver` (query-level date) -> `TimeIntentClassifier` (publish_time vs event_time) -> `compute_retrieval_scope` / `compute_answer_scope_date` in `temporal_scope` is clear. `follow_up_type` (time_switch / event_continue / object_switch) is passed from RouteLLM or rule fallback and used consistently.
- **Verification:** `AnswerVerifier` runs fabrication -> on_topic -> temporal alignment; result and `failure_reason` are passed back to pipeline for replacement message and tracer. Integration is consistent.

---

## 2. Strengths

- **Schemas:** Pydantic models (`TemporalContext`, `RouteLLMOutput`, `ClassificationResult`, `RewriteResult`, `VerificationResult`) are well-defined and used consistently. `classification_from_route_output()` keeps a single source of truth for converting RouteLLM output to classification.
- **Router:** `decide()` receives `effective_last_category` from pipeline (derived from history), enabling context-drift reset and search-loop protection without relying on stale state. `_build_search_params` correctly merges retrieval scope from `compute_retrieval_scope`.
- **Pipeline tracer:** Step-by-step trace (input, temporal, time intent, rewrite, route_llm, route decision, search, GLM prompt/output, verification) supports debugging and auditing.
- **Separation of concerns:** `temporal_scope` only consumes `TemporalContext` and `follow_up_type`; no extra time sources. Score tool is file-only, no LLM.

---

## 3. Issues & Recommendations

### 3.1 Router.route_and_update_state() missing effective_last_category

- **Location:** `app/services/router.py` – `route_and_update_state()` calls `decide()` without `effective_last_category`.
- **Impact:** If this method is ever used, context-drift detection and correct “last turn category” semantics would not apply. `docs/STATE_AND_DELETE_QA.md` already states that callers should pass last_turn_category.
- **Recommendation:** Add parameter `effective_last_category: Optional[str] = None` to `route_and_update_state` and pass it into `decide()`, or document that this method is legacy and should not be used without passing last_turn_category from history.

### 3.2 Answer verifier fallback on LLM failure

- **Location:** `app/services/answer_verifier.py` – `_verify_no_fabrication` and `_verify_on_topic` catch exceptions and return `True` (treat as pass).
- **Impact:** Failures (e.g. network, timeout) are silently treated as “passed”, which weakens verification guarantees.
- **Recommendation:** Per project rule “LET IT CRASH” for non-syntax bugs: either remove the try/except and let exceptions propagate, or return a dedicated result (e.g. `VerificationResult.fail` with a new reason like `VERIFICATION_ERROR`) and let pipeline decide whether to replace the answer.

### 3.3 Hardcoded / machine-specific path in score_tool

- **Location:** `app/services/tools/score_tool.py` – `_DEFAULT_LEGACY_PATH` is a Windows absolute path; also used as fallback in `_get_config_paths()` when env/config is missing.
- **Impact:** Fails on other machines or in CI; default is not portable.
- **Recommendation:** Do not default to an absolute path; if no config/env is set, raise or return empty/None so callers handle “no data” explicitly.

### 3.4 Pipeline docstring vs implementation

- **Location:** `app/services/pipeline.py` ~L404 – Docstring says "UserInput -> QueryRewriter -> **IntentClassifier** -> Router -> ...".
- **Recommendation:** Update to "QueryRewriter -> **RouteLLM** -> Router" (and optionally "IntentClassifier" only where it is still used, if any).

### 3.5 Config: ProductionConfig class-level if

- **Location:** `app/config.py` – `if not SQLALCHEMY_DATABASE_URI: raise ValueError(...)` and CORS assignment run at class definition time.
- **Impact:** Minor: if `DATABASE_URL` is later set via env after import, the check has already run. Usually acceptable; just be aware.
- **Recommendation:** No change required unless you want validation at app startup instead of import time.

---

## 4. Consistency & Style

- **Logging:** Router uses a dedicated file logger (`_router_file_logger`) to `logs/router_YYYY-MM-DD.log`; pipeline uses loguru. Consistent within each component.
- **Chinese in prompts:** RouteLLM, QueryRewriter, AnswerVerifier, and TimeIntentClassifier use Chinese in system/user prompts. Acceptable for a Chinese-facing product; consider extracting to i18n if you add other locales.
- **Method comments:** User rule requires the first sentence of method comments to be product-facing; most modules comply. A few helpers (e.g. `_normalize_reference_date`) are implementation-focused; acceptable for private helpers.

---

## 5. Suggested Commits (Pure English)

Group uncommitted changes into logical commits with conventional, English-only messages. Adjust file lists to match your actual `git status`.

### Option A – Fewer, coarser commits

```text
feat(route): add RouteLLM for routing and follow-up time type

- Add route_llm module: invoke with current utterance and last filter category.
- Output need_retrieval, need_scores, filter_category, time_sensitivity, follow_up_time_type.
- Router uses RouteLLM output; pipeline replaces IntentClassifier with RouteLLM in main flow.
- Add abbreviated prompt when token estimate exceeds LOCAL_LLM_MAX_CONTEXT_TOKENS.
```

**Files (conceptually):** `app/services/route_llm.py` (new), `app/services/router.py`, `app/services/pipeline.py`, `app/services/schemas.py` (RouteLLMOutput, classification_from_route_output), `app/services/pipeline_tracer.py` (record_route_llm).

---

```text
feat(temporal): add time intent and scope layer for retrieval and answer scope

- Add temporal_resolver, temporal_scope, time_intent_classifier.
- compute_retrieval_scope() and compute_answer_scope_date() drive search_params and answer_scope_date.
- Router merges retrieval scope into search_params; pipeline passes follow_up_type and time_intent.
```

**Files:** `app/services/temporal_resolver.py`, `app/services/temporal_scope.py`, `app/services/time_intent_classifier.py` (new), `app/services/schemas.py` (TemporalContext, TimeIntent, FollowUpType), `app/services/router.py` (retrieval scope), `app/services/pipeline.py` (temporal + time intent wiring), `app/services/pipeline_tracer.py` (temporal/time intent steps).

---

```text
feat(verify): add answer verifier for fabrication, on-topic, and temporal alignment

- Add answer_verifier module with VerifyFailureReason and VerificationResult.
- Pipeline runs verification after generation and replaces answer on failure.
- Tracer records verification result and per-step timings.
```

**Files:** `app/services/answer_verifier.py` (new), `app/services/pipeline.py` (verify + replacement), `app/services/pipeline_tracer.py` (record_glm_output verification_result).

---

```text
feat(tools): add NBA score tool and wire tool_scores action

- Add tools/score_tool: read from JSON or parsed_boxscore dir; filter by query team aliases.
- Router returns action tool_scores for scores-only; pipeline executes and returns structured scores.
```

**Files:** `app/services/tools/__init__.py`, `app/services/tools/score_tool.py`, `app/services/schemas.py` (tool-related if any), `app/services/router.py` (tool_scores), `app/services/pipeline.py` (tool_scores execution).

---

```text
feat(rewrite): add RewriteResult with reasoning and category hint to rewriter

- Query rewriter returns RewriteResult(standalone_query, reasoning).
- Pipeline and tracer pass reasoning; LLM user prompt includes rewrite_reasoning and original_query.
```

**Files:** `app/services/schemas.py` (RewriteResult), `app/services/query_rewriter.py`, `app/services/pipeline.py`, `app/services/llm_service.py`, `app/services/pipeline_tracer.py`.

---

```text
chore(config): add pipeline and local LLM config, ignore logs and .cursor

- Add PIPELINE_*, RETRIEVAL_*, CONTEXT_RELEVANCE_THRESHOLD, LOCAL_LLM_*, NBA_*.
- .gitignore: logs/, .cursor/ (if desired).
```

**Files:** `app/config.py`, `.gitignore`.

---

```text
docs: update code review and state/delete QA notes

- Update CODE_REVIEW with RouteLLM, temporal, and verification summary.
- STATE_AND_DELETE_QA: note route_and_update_state should receive last_turn_category.
```

**Files:** `docs/CODE_REVIEW_20260212.md` or new `docs/CODE_REVIEW_*.md`, `docs/STATE_AND_DELETE_QA.md`.

---

### Option B – Finer-grained (example)

If you prefer smaller commits:

- `feat(route): add RouteLLM module and RouteLLMOutput schema`
- `feat(route): wire RouteLLM in pipeline and add route_llm tracer step`
- `feat(temporal): add TemporalResolver and TemporalContext usage`
- `feat(temporal): add temporal_scope and time intent classifier`
- `feat(verify): add AnswerVerifier and VerificationResult`
- `feat(verify): run verification in pipeline and record in tracer`
- `feat(tools): add NBA score tool and tool_scores routing`
- `feat(rewrite): return RewriteResult with reasoning and pass to generation`
- `chore(config): add pipeline and local LLM env config`
- `chore(git): ignore logs and .cursor in .gitignore`
- `docs: update code review and state/delete QA`

---

## 6. Summary

- **Architecture:** RouteLLM-driven routing and temporal/scope layering are clear; verification is integrated end-to-end.
- **Fixes to consider:** Pass `effective_last_category` in `route_and_update_state` if used; avoid silent pass on verifier LLM failure; remove or parameterize hardcoded path in score_tool; align pipeline docstring with RouteLLM.
- **Commits:** Use the suggested English conventional messages above, splitting or merging by your preferred granularity (Option A vs B).
