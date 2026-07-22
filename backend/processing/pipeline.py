"""LLM extraction pipeline: extract structured data from articles via the Batch API.

Reads unprocessed articles from Firestore and submits them to the Anthropic
Message Batches API in chunks of up to 100 requests — 50% cheaper than
synchronous calls, and the added latency is irrelevant on a cron cadence
(ADR 006). Each batch is polled until it ends, then results are mapped back
to articles by Firestore doc ID. If batch submission fails, the pipeline
falls back to synchronous per-article calls. A single article failure never
crashes the run: every failure path logs and records ``processingError`` on
the article doc.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from config import settings
from db.firestore import (
    get_unprocessed_articles,
    set_article_processing_error,
    update_article_after_processing,
)
from processing.prompts import EXTRACTION_PROMPT, build_extraction_message
from processing.utils import (
    call_claude,
    get_batch_results,
    parse_json_object,
    poll_batch,
    submit_batch,
)

logger = logging.getLogger(__name__)

MAX_TOKENS = 4096
MAX_BATCH_REQUESTS = 100

REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "headline",
        "summary",
        "relevance_score",
        "brands_mentioned",
        "vehicles_mentioned",
        "features_extracted",
        "competitive_signal",
        "content_type",
    }
)


def _parse_extraction(text: str) -> dict[str, Any] | None:
    """Parse Claude's response text as JSON and validate the extraction keys.

    Brace-salvage parsing is delegated to
    ``processing.utils.parse_json_object``; this adds the
    extraction-specific required-key validation.
    """
    parsed = parse_json_object(text)
    if parsed is None:
        return None
    missing = REQUIRED_KEYS - parsed.keys()
    if missing:
        logger.error(f"extraction result missing required keys: {sorted(missing)}")
        return None
    return parsed


def _to_article_update(result: dict[str, Any]) -> dict[str, Any]:
    """Map an extraction result to the article-doc update fields.

    ``update_article_after_processing`` expects article schema fields
    (title_en, relevance_score, ...) rather than Claude's raw output keys.
    ``summary`` has no article field and is dropped. ``body_en`` is not
    set: full article translation was removed from the extraction to cut
    per-article cost (see docs/tech-debt.md, 2026-07-22).
    """
    return {
        "title_en": result["headline"],
        "relevance_score": result["relevance_score"],
        "content_type": result["content_type"],
        "brands_mentioned": result["brands_mentioned"],
        "vehicles_mentioned": result["vehicles_mentioned"],
        "features_extracted": result["features_extracted"],
        "competitive_signal": result["competitive_signal"],
    }


async def process_article(article: dict[str, Any]) -> dict[str, Any] | None:
    """Run one article through Claude extraction and return the parsed result.

    Returns the validated extraction dict (raw pipeline output shape from
    docs/llm-pipeline.md), or None on any failure — API errors after retries,
    malformed JSON, or missing required keys. Never raises.
    """
    try:
        message = build_extraction_message(article["title"], article["body"])
    except KeyError as exc:
        logger.error(f"article missing required field {exc} id={article.get('id')}")
        return None
    text = await call_claude(
        [{"role": "user", "content": message}],
        max_tokens=MAX_TOKENS,
        system=EXTRACTION_PROMPT,
    )
    if text is None:
        return None
    return _parse_extraction(text)


class _ChunkStats(NamedTuple):
    """Per-chunk outcome counts accumulated into the pipeline summary."""

    succeeded: int
    failed: int
    errors_recorded: int


def _build_batch_requests(articles: list[dict[str, Any]]) -> list[Request]:
    """Build one batch request per article, keyed by Firestore doc ID.

    Articles missing a title or body are skipped; they receive a
    ``processingError`` later because their doc ID never appears in the
    batch results. The extraction prompt is sent as a system block with a
    ``cache_control`` breakpoint so the fixed prompt is served from the
    prompt cache across the batch.
    """
    requests: list[Request] = []
    for article in articles:
        try:
            message = build_extraction_message(article["title"], article["body"])
        except KeyError as exc:
            logger.error(f"article missing required field {exc} id={article.get('id')}")
            continue
        requests.append(
            Request(
                custom_id=article["id"],
                params=MessageCreateParamsNonStreaming(
                    model=settings.SONNET_MODEL,
                    max_tokens=MAX_TOKENS,
                    system=[
                        {
                            "type": "text",
                            "text": EXTRACTION_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=[{"role": "user", "content": message}],
                ),
            )
        )
    return requests


async def _write_back(article: dict[str, Any], result: dict[str, Any] | None) -> bool:
    """Persist one article outcome to Firestore; return True on success.

    Failures record ``processingError`` and leave ``processed == false``
    so the article stays queued for the next run.
    """
    if result is not None:
        await update_article_after_processing(article["id"], _to_article_update(result))
        return True
    await set_article_processing_error(article["id"], "LLM extraction failed")
    return False


async def _apply_batch_results(
    articles: list[dict[str, Any]], results: dict[str, str | None]
) -> _ChunkStats:
    """Parse and write batch results back, one article at a time.

    An article whose doc ID is missing from ``results`` (skipped at
    request build, or dropped by the API) is treated the same as a failed
    extraction.
    """
    succeeded = 0
    failed = 0
    for article in articles:
        text = results.get(article["id"])
        result = _parse_extraction(text) if text is not None else None
        if await _write_back(article, result):
            succeeded += 1
        else:
            failed += 1
    return _ChunkStats(succeeded, failed, failed)


async def _process_chunk_sync(articles: list[dict[str, Any]]) -> _ChunkStats:
    """Process a chunk with synchronous per-article Claude calls (fallback path)."""
    succeeded = 0
    failed = 0
    for article in articles:
        result = await process_article(article)
        if await _write_back(article, result):
            succeeded += 1
        else:
            failed += 1
    return _ChunkStats(succeeded, failed, failed)


async def _process_batch_chunk(articles: list[dict[str, Any]]) -> _ChunkStats:
    """Run one chunk of up to ``MAX_BATCH_REQUESTS`` articles through the Batch API.

    Submission failure falls back to synchronous processing. A poll
    timeout or results-streaming failure counts the chunk as failed
    without recording per-article errors: those are batch-level
    transients, and the articles stay queued for the next run.
    """
    requests = _build_batch_requests(articles)
    if not requests:
        return await _apply_batch_results(articles, {})
    try:
        batch_id = await submit_batch(requests)
    except anthropic.AnthropicError as exc:
        logger.warning(f"batch submission failed ({exc}); falling back to synchronous processing")
        return await _process_chunk_sync(articles)
    batch = await poll_batch(batch_id)
    if batch is None:
        logger.error(f"batch {batch_id} timed out; articles stay queued for the next run")
        return _ChunkStats(0, len(articles), 0)
    try:
        results = await get_batch_results(batch_id)
    except anthropic.AnthropicError as exc:
        logger.error(f"batch results retrieval failed for {batch_id}: {exc}")
        return _ChunkStats(0, len(articles), 0)
    return await _apply_batch_results(articles, results)


async def run_pipeline(batch_size: int = 50) -> dict[str, int]:
    """Process up to ``batch_size`` unprocessed articles via the Batch API.

    Articles are submitted in sequential batches of up to
    ``MAX_BATCH_REQUESTS`` requests each. Successful extractions are
    written back to the article doc (mapped to article schema fields) and
    failures are recorded via ``set_article_processing_error``, leaving
    the article queued for the next run. Returns a summary of
    total/succeeded/failed counts plus the number of processing errors
    recorded on article docs.
    """
    articles = await get_unprocessed_articles(limit=batch_size)
    total = len(articles)
    if total == 0:
        logger.info("no unprocessed articles")
        return {"total": 0, "succeeded": 0, "failed": 0, "processing_errors": 0}
    logger.info(f"processing {total} unprocessed articles")

    succeeded = 0
    failed = 0
    errors_recorded = 0
    for start in range(0, total, MAX_BATCH_REQUESTS):
        stats = await _process_batch_chunk(articles[start : start + MAX_BATCH_REQUESTS])
        succeeded += stats.succeeded
        failed += stats.failed
        errors_recorded += stats.errors_recorded

    logger.info(
        f"pipeline summary: total={total} succeeded={succeeded} failed={failed} "
        f"processing_errors={errors_recorded}"
    )
    return {
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "processing_errors": errors_recorded,
    }
