# ADR 006: Batch API Adoption for the Extraction Pipeline

## Status
Accepted

## Context
The extraction pipeline processes every unprocessed article with a synchronous Claude API call, one at a time. After removing full translation (docs/tech-debt.md, 2026-07-22) the per-article cost sits at ~$0.021, which is still too high for the budget at the target ingestion volume.

The Anthropic Message Batches API processes the same requests at 50% off standard token prices with no quality difference — the only tradeoff is timing: batches complete asynchronously (typically within an hour, 24-hour maximum) instead of returning in seconds. The pipeline runs on a 6-hour cron and nothing downstream needs real-time results, so latency is not a constraint. Prompt caching on the extraction system prompt continues to work inside batch requests.

Alternatives considered: keep the synchronous path (no work, but forgoes the single largest available cost reduction) and concurrent synchronous calls (faster, but saves nothing).

## Decision
Switch the extraction pipeline to the Message Batches API. Keep the synchronous path as a fallback and for signal narrative generation.

- Three new helpers in `backend/processing/utils.py`: `submit_batch` (create), `poll_batch` (retrieve every 30 seconds, 2-hour timeout), and `get_batch_results` (stream results keyed by `custom_id`).
- `run_pipeline` builds one request per article with the Firestore doc ID as `custom_id`, capped at 100 requests per batch; larger queues are submitted as sequential batches. Each request carries the cached extraction system prompt and the Sonnet model — model selection is unchanged.
- `call_claude` is untouched: it remains the synchronous helper used by Phase 2 signal narrative generation and entity resolution, and serves as the extraction fallback when batch submission fails.
- Parse/validate/Firestore-update logic per article is unchanged; a poll timeout leaves articles queued (`processed == false`) for the next run rather than recording per-article errors.

## Consequences
- Per-article extraction cost drops ~50% (~$0.021 to ~$0.011) with no quality change.
- Pipeline latency increases from seconds per article to minutes per batch. Acceptable for a cron-based pipeline; Phase 2 processing that runs after the pipeline simply starts later in the same run.
- Polling adds complexity: batch status handling, result-to-article mapping by `custom_id`, and timeout behavior all need dedicated tests (`tests/processing/`).
- The synchronous fallback is a safety net but means a Batch API outage silently doubles costs — tracked in docs/tech-debt.md with a plan to add monitoring/alerting on fallback triggers.
- Tests mock `client.messages.batches.*` in addition to `client.messages.create`.
