"""LLM extraction pipeline: translate and extract structured data from articles.

Reads unprocessed articles from Firestore, sends each to Claude Sonnet for
translation and structured extraction, and writes results back. Articles are
processed sequentially — one at a time — to respect API rate limits and keep
debugging simple. A single article failure never crashes the run: every
failure path logs and records ``processingError`` on the article doc.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import anthropic

from config import settings
from db.firestore import (
    get_unprocessed_articles,
    set_article_processing_error,
    update_article_after_processing,
)
from processing.prompts import build_extraction_message

logger = logging.getLogger(__name__)

MAX_TOKENS = 4096

REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "english_translation",
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


async def _call_claude(client: anthropic.AsyncAnthropic, message: str) -> str | None:
    """Call the Claude API with retries and return the response text.

    Retries with exponential backoff on any anthropic exception, up to
    ``settings.MAX_RETRIES`` retries after the initial attempt. Returns None
    once all attempts are exhausted or the response has no content.
    """
    for attempt in range(settings.MAX_RETRIES + 1):
        try:
            response = await client.messages.create(
                model=settings.SONNET_MODEL,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": message}],
            )
        except anthropic.AnthropicError as exc:
            logger.warning(
                f"claude api call failed attempt={attempt + 1}/{settings.MAX_RETRIES + 1} "
                f"error={exc}"
            )
            if attempt < settings.MAX_RETRIES:
                await asyncio.sleep(2**attempt)
            continue
        if not response.content:
            logger.error("claude response has no content blocks")
            return None
        block = response.content[0]
        if block.type != "text":
            logger.error(f"unexpected first content block type: {block.type}")
            return None
        return block.text
    logger.error(f"claude api call failed after {settings.MAX_RETRIES + 1} attempts")
    return None


def _parse_extraction(text: str) -> dict[str, Any] | None:
    """Parse Claude's response text as JSON, salvaging embedded JSON if needed.

    Claude occasionally wraps the JSON in preamble or trailing text; the
    fallback slices from the first ``{`` to the last ``}`` and re-parses.
    """
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            logger.error("claude response contains no JSON object")
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            logger.error(f"failed to parse claude response as JSON: {exc}")
            return None
    if not isinstance(parsed, dict):
        logger.error(f"claude response is not a JSON object: {type(parsed).__name__}")
        return None
    missing = REQUIRED_KEYS - parsed.keys()
    if missing:
        logger.error(f"extraction result missing required keys: {sorted(missing)}")
        return None
    return parsed


def _to_article_update(result: dict[str, Any]) -> dict[str, Any]:
    """Map an extraction result to the article-doc update fields.

    ``update_article_after_processing`` expects article schema fields
    (title_en, body_en, ...) rather than Claude's raw output keys.
    ``summary`` has no article field and is dropped.
    """
    return {
        "title_en": result["headline"],
        "body_en": result["english_translation"],
        "relevance_score": result["relevance_score"],
        "content_type": result["content_type"],
        "brands_mentioned": result["brands_mentioned"],
        "vehicles_mentioned": result["vehicles_mentioned"],
        "features_extracted": result["features_extracted"],
        "competitive_signal": result["competitive_signal"],
    }


async def process_article(
    article: dict[str, Any], client: anthropic.AsyncAnthropic
) -> dict[str, Any] | None:
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
    text = await _call_claude(client, message)
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
    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    articles = await get_unprocessed_articles(limit=batch_size)
    total = len(articles)
    logger.info(f"processing {total} unprocessed articles")

    succeeded = 0
    failed = 0
    for index, article in enumerate(articles, start=1):
        result = await process_article(article, client)
        if result is not None:
            await update_article_after_processing(article["id"], _to_article_update(result))
            succeeded += 1
        else:
            await set_article_processing_error(article["id"], "LLM extraction failed")
            failed += 1
        label = article.get("title") or article.get("source_url")
        logger.info(f"Processed {index}/{total}: {label}")

    return {"total": total, "succeeded": succeeded, "failed": failed}
