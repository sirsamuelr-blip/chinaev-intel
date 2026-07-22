# LLM Processing Pipeline

## Overview

Reads unprocessed articles from Firestore (`processed == false`), sends each to the Claude API for translation and structured extraction, writes results back to the article doc, and creates/updates entries in `features`, `brands`, and `vehicles` collections as needed.

File: `backend/processing/pipeline.py`
Prompts: `backend/processing/prompts.py`

## Model Selection

- **Claude Sonnet** (`claude-sonnet-4-6`): All bulk article processing. Cost target ~$0.04/article.
- **Claude Opus** (`claude-opus-4-6`): Weekly digest generation only (Phase 4). Needs synthesis and editorial voice.
- **Claude Haiku** (`claude-haiku-4-5-20251001`): Relevance triage pre-filter only (see Pre-filtering below). ~$0.003/article.

Do not use Opus for article processing. Do not use Sonnet for digest generation. Do not use Haiku for anything beyond triage.

> **Prompt caching:** Prompt caching is enabled on the extraction prompt. The system prompt is cached across sequential article processing calls, reducing input token cost by ~90% on the fixed prompt portion. Note: caching only engages once the cached prefix meets the model's minimum cacheable length (2048 tokens on Sonnet 4.6); below that the `cache_control` marker is a silent no-op (no error, no extra cost). Verify via `usage.cache_read_input_tokens` in API responses.

## Pre-filtering (Triage)

Before full extraction, every unprocessed article gets a cheap synchronous Haiku call (`TRIAGE_PROMPT`, per-article via `call_claude` — not batched; at ~$0.003/article the Batch API overhead is not worth it). Articles scoring `relevance_score >= RELEVANCE_THRESHOLD` (4, in `backend/config/settings.py`) proceed to the Sonnet extraction batch. Articles scoring below are marked processed with the triage results only — `titleEn` (from headline), `relevanceScore`, `contentType`, `triageOnly: true`, `processed: true` — and skip full extraction entirely. Triage-only docs carry no `featuresExtracted`, `brandsMentioned`, `vehiclesMentioned`, or `competitiveSignal`. See ADR 007.

This is the `TRIAGE_PROMPT` template in `backend/processing/prompts.py`:

```
You are an automotive intelligence analyst. Given this article about the
Chinese EV/auto industry, extract the following:

1. headline: One-line English headline.
2. relevance_score: 1-10 (10 = directly about software/AI/UX features
   that Western OEMs should know about).
3. content_type: One of: news, review, teardown, forum_post, opinion,
   regulatory, earnings.

Respond in JSON only. No markdown fences. No preamble.
```

> **Fail open:** Any triage failure — API error after retries, malformed/non-JSON response, missing keys, non-integer relevance_score, missing title/body — sends the article to full extraction. We would rather waste ~$0.02 on a low-relevance article than miss a high-value one. Triage never sets `processingError`.

> **Caching note:** Haiku 4.5's minimum cacheable prefix is 4096 tokens; the short triage prompt is well below it, so the `cache_control` marker on the triage system prompt is a silent no-op (no error, no extra cost) — same mechanics as the Sonnet note above.

## Extraction Prompt

This is the template in `backend/processing/prompts.py`:

```
You are an automotive intelligence analyst. Given this article about the
Chinese EV/auto industry, extract the following:

1. headline: One-line English headline.
2. summary: 2-3 sentence English summary.
3. relevance_score: 1-10 (10 = directly about software/AI/UX features
   that Western OEMs should know about).
4. brands_mentioned: List of brand name strings.
5. vehicles_mentioned: List of specific model name strings.
6. features_extracted: Array of objects, each with:
   - feature_name (string)
   - category (string, one of: adas, ai_assistant, infotainment,
     connectivity, ota, battery_software, cockpit_ux)
   - description (string)
   - supplier (string or null)
   - is_new (boolean, true if this is a new launch/announcement)
7. competitive_signal: If this has implications for Western OEMs,
   1-2 sentences. Otherwise null.
8. content_type: One of: news, review, teardown, forum_post, opinion,
   regulatory, earnings.

Respond in JSON only. No markdown fences. No preamble.
```

> **Note:** Full article translation was removed to reduce per-article API cost from ~$0.10 to ~$0.03. Translation will be added as an on-demand feature in Phase 5.

## Pipeline Input Shape

What goes into the pipeline from the scraper output / Firestore article doc:

```python
{
    "source_name": str,
    "source_url": str,
    "title": str,
    "body": str,
    "publish_date": str,      # ISO 8601
    "scrape_date": str,       # ISO 8601
    "language": str,          # "zh" or "en"
}
```

## Pipeline Output Shape

What Claude returns (JSON):

```python
{
    "headline": str,
    "summary": str,
    "relevance_score": int,             # 1-10
    "brands_mentioned": list[str],
    "vehicles_mentioned": list[str],
    "features_extracted": list[dict],   # see below
    "competitive_signal": str | None,
    "content_type": str,
}
```

Each item in `features_extracted`:

```python
{
    "feature_name": str,
    "category": str,          # one of the 7 feature categories
    "description": str,
    "supplier": str | None,
    "is_new": bool,
}
```

## Pipeline Flow

> **Batch API:** The pipeline uses the Anthropic Message Batches API for 50% cost reduction. Batch requests are polled every 30 seconds with a 2-hour timeout. If batch submission fails, the pipeline falls back to synchronous processing.

1. Query Firestore: articles where `processed == false`, ordered by `scrapeDate` asc.
2. Triage each fetched article with Haiku (see Pre-filtering above). Below-threshold articles are marked processed with `triageOnly = true` and excluded from extraction; triage failures fail open into the batch.
3. Build one batch request per passed article, using the Firestore doc ID as the `custom_id`. Each request sends `EXTRACTION_PROMPT` as the system prompt (with a `cache_control` breakpoint) and the article title and body as the user message. Model: Sonnet. JSON response requested.
4. Submit the batch via the Message Batches API, capped at 100 requests per batch. More than 100 passed articles are submitted as multiple batches, sequentially.
5. Poll the batch every 30 seconds until its status is `ended` (2-hour timeout).
6. Stream the batch results and map them back to articles by `custom_id`.
7. For each result: parse JSON, validate against expected schema, then update the article doc: set `titleEn`, `relevanceScore`, `contentType`, `brandsMentioned`, `vehiclesMentioned`, `featuresExtracted`, `processed = true`. (`bodyEn` is not populated — see note above.)
8. If `features_extracted` has items with `is_new == true`, create entries in `features` collection.
9. Log summary: total processed, successes, failures, processing errors, triage-skipped count.

## Error Handling

- Triage failure of any kind (API error, malformed response, missing keys, bad score type): fail open — the article is included in the extraction batch. Triage never records `processingError`.
- Invalid JSON from Claude: log error, set `processingError` field on article doc. Do NOT set `processed = true`. Skip to next article.
- Batch submission failure: log a warning and fall back to synchronous per-article processing (full price) for that batch.
- Batch poll timeout or results retrieval failure: log error; affected articles keep `processed == false` with no `processingError`, so they are retried on the next run.
- Errored/canceled/expired batch result, or a doc ID missing from the results: treated as a failed extraction — set `processingError`, do NOT set `processed = true`.
- API failure on the synchronous fallback path (rate limit, timeout, server error): retry with exponential backoff, max 3 retries.
- Single article failure must never crash the pipeline run.

## Automotive Glossary

File: `backend/config/glossary.json`

Common mistranslations to watch for and validate against:

| Chinese | Correct English | Common Mistranslation |
|---|---|---|
| 智能驾驶 | intelligent driving / ADAS | smart driving |
| 座舱 | cockpit | cabin |
| 大模型 | large language model / LLM | big model |
| 城市NOA | city navigate-on-autopilot | city NOA (untranslated) |
| 激光雷达 | LiDAR | laser radar |
| 智能座舱 | smart cockpit | intelligent cabin |
| 域控制器 | domain controller | area controller |
| 线控底盘 | drive-by-wire chassis | wire-controlled chassis |
| 算力 | compute power | calculation power |
| 高阶智驾 | advanced intelligent driving | high-level smart driving |
