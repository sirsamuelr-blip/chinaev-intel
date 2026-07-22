"""LLM extraction pipeline: translate and extract structured data from articles.

Reads unprocessed articles from Firestore, sends each to Claude Sonnet for
translation and structured extraction, and writes results back. Articles are
processed sequentially — one at a time — to respect API rate limits and keep
debugging simple. A single article failure never crashes the run: every
failure path logs and records ``processingError`` on the article doc.
"""

from __future__ import annotations

import logging
from typing import Any

from db.firestore import (
    get_unprocessed_articles,
    set_article_processing_error,
    update_article_after_processing,
)
from processing.prompts import EXTRACTION_PROMPT, build_extraction_message
from processing.utils import call_claude, parse_json_object

logger = logging.getLogger(__name__)

MAX_TOKENS = 4096

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


async def run_pipeline(batch_size: int = 50) -> dict[str, int]:
    """Process up to ``batch_size`` unprocessed articles sequentially.

    Successful extractions are written back to the article doc (mapped to
    article schema fields) and failures are recorded via
    ``set_article_processing_error``, leaving the article in the queue for
    the next run. Returns a summary of total/succeeded/failed counts.
    """
    articles = await get_unprocessed_articles(limit=batch_size)
    total = len(articles)
    logger.info(f"processing {total} unprocessed articles")

    succeeded = 0
    failed = 0
    for index, article in enumerate(articles, start=1):
        result = await process_article(article)
        if result is not None:
            await update_article_after_processing(article["id"], _to_article_update(result))
            succeeded += 1
        else:
            await set_article_processing_error(article["id"], "LLM extraction failed")
            failed += 1
        label = article.get("title") or article.get("source_url")
        logger.info(f"Processed {index}/{total}: {label}")

    return {"total": total, "succeeded": succeeded, "failed": failed}
