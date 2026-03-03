# Code Review: miniprogram-server

**Date:** 2026-03-03  
**Scope:** Server application (`app/`), config, services, API blueprints  
**Perspective:** External, maintenance-oriented, correctness and consistency focus  

---

## 1. Executive Summary

The miniprogram-server is a Flask backend for a WeChat miniprogram, providing unified chat (RAG + direct generation), temporal parsing, follow-up handling, and score lookup. The Pipeline orchestration is well-structured with RouteLLM, Router, and AnswerVerifier integration. Several security, code quality, and performance issues should be addressed before production.

---

## 2. Code Quality

### 2.1 Duplicate Auth Helper (Medium)

**Location:** `app/api/chat.py`, `app/api/rag.py`, `app/api/sync.py`, `app/api/conversations.py`

The same `_get_openid_from_token()` is duplicated across four blueprints.

**Recommendation:** Move to shared helper (e.g. `app/utils/jwt_auth.py` or `app/middlewares/auth.py`).

### 2.2 Broad Exception Handling (Medium)

**Locations:** Multiple API blueprints

Token parsing uses `except Exception` and returns `None`, hiding `jwt.ExpiredSignatureError`, `jwt.InvalidTokenError`, etc.

**Recommendation:** Catch only `jwt.PyJWTError` and log/re-raise others.

### 2.3 Pipeline Docstring vs Implementation

Docstring refers to "IntentClassifier"; implementation uses RouteLLM. Ensure documentation matches current flow.

---

## 3. Security

### 3.1 Sensitive Defaults (High)

**Location:** `app/config.py`

- `SECRET_KEY`, `JWT_SECRET_KEY` have weak dev defaults. In production, missing env vars should raise instead of falling back.

### 3.2 Unauthenticated RAG Info (High)

**Location:** `app/api/rag.py` `/api/rag/info`

`/api/rag/info` has no auth while `/api/rag/query` and other RAG endpoints do.

### 3.3 Authorization Bug in Conversations (High)

**Location:** `app/api/conversations.py` `append_messages`

Conversations are fetched by `chat_id` only. Any user with a valid JWT can modify another user's conversation.

**Recommendation:** Filter by `chat_id` and `openid`:
```python
conv = Conversation.query.filter_by(chat_id=chat_id, openid=openid).first()
```

### 3.4 CORS Default

Default `*` is too permissive for production. Use explicit origins (e.g. miniprogram domains).

### 3.5 No Rate Limiting

No rate limiting on `/api/auth/login`, `/api/chat`, etc. Risk of brute force and abuse.

---

## 4. Performance

### 4.1 HTTP Client Reuse (Medium)

**Location:** `app/services/llm_service.py`

`chat_stream` creates a new `httpx.Client` per call instead of reusing the instance client. Use shared client for connection pooling.

### 4.2 Client Lifecycle

`LLMService` has `close()` but no Flask teardown registers it. Long-lived workers may hold connections indefinitely.

### 4.3 Embedding Caching

Same query can trigger multiple embedding calls (RouteLLM, QueryRewriter, retrieval). Consider in-memory or Redis cache for recent queries.

---

## 5. Best Practices

### 5.1 Empty Test Suite

No unit or integration tests for auth, pipeline, RAG.

### 5.2 Hardcoded Paths

**Locations:** `app/config.py`, `app/services/tools/score_tool.py`

Machine-specific paths like `C:\Users\HX\...` should be removed. Use env/config only.

### 5.3 Config Validation Timing

Production config validation runs at class definition time, before env may be loaded. Run in `create_app()` or startup instead.

---

## 6. Potential Bugs

### 6.1 Empty Choices in LLM Response

**Location:** `app/services/llm_service.py`

`result["choices"][0]` can raise `IndexError` if choices is empty. Add defensive check.

### 6.2 datetime.utcnow() Deprecation

**Location:** `app/utils/jwt_auth.py`

Use `datetime.now(timezone.utc)` instead of deprecated `datetime.utcnow()`.

---

## 7. Priority Summary

| Priority | Item |
|----------|------|
| P0 | Fix conversation ownership check in `append_messages` |
| P0 | Require SECRET_KEY and JWT_SECRET_KEY in production, no defaults |
| P0 | Add auth to `/api/rag/info` or document intent |
| P1 | Add rate limiting for auth and chat |
| P1 | Refactor `_get_openid_from_token()` to shared helper |
| P2 | Reuse httpx.Client and register teardown |
| P2 | Add tests, remove hardcoded paths |
