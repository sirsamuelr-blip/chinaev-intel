# LLM Processing Pipeline

## Overview

Reads unprocessed articles from Firestore (`processed == false`), sends each to the Claude API for translation and structured extraction, writes results back to the article doc, and creates/updates entries in `features`, `brands`, and `vehicles` collections as needed.

File: `backend/processing/pipeline.py`
Prompts: `backend/processing/prompts.py`

## Model Selection

- **Claude Sonnet** (`claude-sonnet-4-6`): All bulk article processing. Cost target ~$0.04/article.
- **Claude Opus** (`claude-opus-4-6`): Weekly digest generation only (Phase 4). Needs synthesis and editorial voice.

Do not use Opus for article processing. Do not use Sonnet for digest generation.

## Extraction Prompt

This is the template in `backend/processing/prompts.py`:

```
You are an automotive intelligence analyst. Given this article about the
Chinese EV/auto industry, extract the following:

1. english_translation: Full English translation of the article.
   If the article is already in English, return the original text.
2. headline: One-line English headline.
3. summary: 2-3 sentence English summary.
4. relevance_score: 1-10 (10 = directly about software/AI/UX features
   that Western OEMs should know about).
5. brands_mentioned: List of brand name strings.
6. vehicles_mentioned: List of specific model name strings.
7. features_extracted: Array of objects, each with:
   - feature_name (string)
   - category (string, one of: adas, ai_assistant, infotainment,
     connectivity, ota, battery_software, cockpit_ux)
   - description (string)
   - supplier (string or null)
   - is_new (boolean, true if this is a new launch/announcement)
8. competitive_signal: If this has implications for Western OEMs,
   1-2 sentences. Otherwise null.
9. content_type: One of: news, review, teardown, forum_post, opinion,
   regulatory, earnings.

Respond in JSON only. No markdown fences. No preamble.
```

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
    "english_translation": str,
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

1. Query Firestore: articles where `processed == false`, ordered by `scrapeDate` asc.
2. For each article, combine `EXTRACTION_PROMPT` with the article title and body.
3. Call Claude API (Sonnet). Request JSON response.
4. Parse JSON. Validate against expected schema.
5. Update article doc: set `titleEn`, `bodyEn`, `relevanceScore`, `contentType`, `brandsMentioned`, `vehiclesMentioned`, `featuresExtracted`, `processed = true`.
6. If `features_extracted` has items with `is_new == true`, create entries in `features` collection.
7. Log: success/failure, token count, processing duration.

## Error Handling

- Invalid JSON from Claude: log error, set `processingError` field on article doc. Do NOT set `processed = true`. Skip to next article.
- API failure (rate limit, timeout, server error): retry with exponential backoff, max 3 retries.
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
