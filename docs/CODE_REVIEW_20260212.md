# Code Review: RAG Pipeline & Classification Updates

**Scope:** `app/services/` — schemas, pipeline, intent_classifier, query_rewriter, llm_service, vector_store, router, pipeline_tracer.

## Summary

- **Schemas:** `RewriteResult(standalone_query, reasoning)`; `ClassificationResult.filter_categories` (top-k, max 3) with `filter_category` kept as first element for compatibility.
- **Pipeline:** Classification-before-rewrite; rewrite receives `category_hint`; dual-track retrieval (original + rewritten query) with RRF merge; rewrite-confidence weighting when similarity &lt; 0.75; `asymmetry_note` for sub-intent coverage; direct-generation system prompt includes few-shot for price/weight conversion (oz→g, USD→CNY) with concise output rules.
- **Intent classifier:** Stage 2 outputs `filter_categories` (top 3); `_normalize_categories` enforces validity and length; `CATEGORY_OPTIONS` exposed for pipeline tracer; rule fallback fills `filter_categories`.
- **Vector store:** `search` / `search_with_expansion` accept `filter_categories`; filter via `MatchAny(any=cats)` with `general` appended when not present.
- **Router:** Passes `filter_categories` (up to 3) into search params; `effective_filter_categories` used for logging and search.
- **Query rewriter:** Returns `RewriteResult`; prompt includes `category_constraint` from `category_hint`; parses "改写原因" into `reasoning`.
- **LLM service:** `_build_news_user_prompt` adds `original_query` and `rewrite_reasoning` for grounding; `generate_answer` / `generate_answer_stream` accept and forward them.
- **Pipeline tracer:** `record_rewrite` takes optional `reasoning`.

## Consistency

- `filter_categories` flows: classifier → router → vector_store; single-category legacy path retained where no top-k.
- Rewrite reasoning and original query flow from rewriter → pipeline → tracer and into the generation user prompt.

## Notes

- Git CRLF warnings on Windows are expected; no change to logic.
