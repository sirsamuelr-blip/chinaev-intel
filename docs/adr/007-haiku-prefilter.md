# ADR 007: Haiku Pre-filter for the Extraction Pipeline

## Status
Accepted

## Context
Roughly 40-50% of scraped articles score below 4 on relevance — export statistics, executive appointments, general industry commentary. They cost the same to extract as a high-value ADAS feature launch (~$0.011/article after Batch API adoption, ADR 006), yet none of their extracted structure is ever used downstream. That is wasted spend at the target ingestion volume.

Claude Haiku 4.5 (`claude-haiku-4-5-20251001`, $1/$5 per million tokens vs Sonnet's $3/$15) can score an article's relevance for ~$0.003. A triage call returning only headline, relevance_score, and content_type lets the pipeline skip full Sonnet extraction for low-relevance articles, saving an estimated 40-60% of Sonnet calls depending on source mix.

Alternatives considered: keyword pre-filters in the scrapers (brittle, bilingual, and per-source maintenance burden), triaging inside the Sonnet batch itself (saves nothing — the expensive call still runs), and doing nothing.

## Decision
Add a synchronous per-article Haiku triage step (`triage_articles` in `backend/processing/pipeline.py`) that runs before batch submission.

- A minimal `TRIAGE_PROMPT` requests only headline, relevance_score (same 1-10 criteria as the extraction prompt), and content_type — minimal output tokens are the point.
- `relevance_score >= RELEVANCE_THRESHOLD` (4, in `backend/config/settings.py`) proceeds to the Sonnet extraction batch.
- Below-threshold articles are marked processed with the triage results only — `titleEn`, `relevanceScore`, `contentType`, plus `triageOnly: true` — and skip full extraction. The field is additive; no Firestore schema change.
- Any triage failure (API error after retries, malformed JSON, missing keys, non-integer score, missing title/body) fails open: the article goes to full extraction. We would rather waste ~$0.02 on a low-relevance article than miss a high-value one.
- Triage uses the existing synchronous `call_claude` helper, not the Batch API — Haiku is fast and cheap enough that batch overhead is not worth it for a $0.003 call. The Firestore write reuses `update_article_after_processing`.

## Consequences
- Sonnet batch spend drops an estimated 40-60% depending on source mix; each skipped article costs ~$0.003 of Haiku instead of ~$0.011 of batched Sonnet.
- Low-relevance articles never get `featuresExtracted`, `brandsMentioned`, `vehiclesMentioned`, or `competitiveSignal`. The `triageOnly` field distinguishes them from fully processed articles.
- Phase 2 signal detection and entity promotion should skip `triageOnly` articles. Today they tolerate the missing fields via defensive `.get(...) or []` access and simply see empty brand/feature lists, so no Phase 2 changes ship with this ADR — the explicit skip is a follow-up (tracked in docs/tech-debt.md).
- Adds a processing step: n sequential Haiku calls per run (~1-2 minutes for a 50-article queue). Acceptable on a 6-hour cron.
- Fail-open means a Haiku outage degrades gracefully to pre-ADR-007 behavior (everything extracted at full cost) rather than dropping articles.
- The triage prompt is below Haiku 4.5's 4096-token minimum cacheable prefix, so the `cache_control` marker attached by `call_claude` never engages (silent no-op — no error, no extra cost).
- Tests mock triage alongside the batch helpers; the pipeline summary gains a `triage_skipped` count.
