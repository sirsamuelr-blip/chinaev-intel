"""Shared helpers for the LLM processing modules.

``call_claude`` is the single Claude API retry helper used by the
extraction pipeline fallback, entity promotion, and signal detection —
extracted from the per-module copies tracked in docs/tech-debt.md.
``parse_json_object`` is the shared brace-salvage JSON parser for
Claude responses that wrap JSON in preamble or trailing text.
``submit_batch`` / ``poll_batch`` / ``get_batch_results`` drive the
Message Batches API used by the extraction pipeline (50% cheaper than
synchronous calls; see ADR 006).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import anthropic
from anthropic import Omit, omit
from anthropic.types import MessageParam, TextBlockParam
from anthropic.types.messages import MessageBatch, MessageBatchIndividualResponse
from anthropic.types.messages.batch_create_params import Request

from config import settings

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 4096
BATCH_POLL_INTERVAL_SECONDS = 30
BATCH_TIMEOUT_SECONDS = 7200


async def call_claude(
    messages: list[MessageParam],
    model: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    system: str | None = None,
) -> str | None:
    """Call the Claude API with retries and return the response text.

    ``model`` defaults to ``settings.SONNET_MODEL`` when None. ``system``,
    when given, is sent as a system prompt with a ``cache_control``
    breakpoint so identical prompts are served from the prompt cache
    across sequential calls (5-minute TTL). Retries with exponential
    backoff on any anthropic exception, up to ``settings.MAX_RETRIES``
    retries after the initial attempt. Returns None once all attempts are
    exhausted or the response has no text content.
    """
    resolved_model = model if model is not None else settings.SONNET_MODEL
    system_blocks: list[TextBlockParam] | Omit = omit
    if system is not None:
        system_blocks = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    for attempt in range(settings.MAX_RETRIES + 1):
        try:
            response = await client.messages.create(
                model=resolved_model,
                max_tokens=max_tokens,
                system=system_blocks,
                messages=messages,
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


async def submit_batch(requests: list[Request]) -> str:
    """Submit a Message Batches API batch and return its ID.

    No internal retry: the SDK already retries transient HTTP errors, and
    callers fall back to synchronous processing when submission fails, so
    exceptions propagate.
    """
    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    batch = await client.messages.batches.create(requests=requests)
    logger.info(f"submitted batch {batch.id} with {len(requests)} requests")
    return batch.id


async def poll_batch(
    batch_id: str,
    poll_interval_seconds: int = BATCH_POLL_INTERVAL_SECONDS,
    timeout_seconds: int = BATCH_TIMEOUT_SECONDS,
) -> MessageBatch | None:
    """Poll a batch until it ends, returning None on timeout.

    Polls every ``poll_interval_seconds`` for up to ``timeout_seconds``.
    The timeout is iteration-counted rather than wall-clock so behavior
    stays deterministic when sleeps are patched in tests. Transient
    retrieve errors are logged and polling continues; the 30-second
    interval doubles as backoff.
    """
    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    max_polls = max(1, timeout_seconds // poll_interval_seconds)
    for _ in range(max_polls):
        try:
            batch = await client.messages.batches.retrieve(batch_id)
        except anthropic.AnthropicError as exc:
            logger.warning(f"batch retrieve failed for {batch_id}: {exc}")
        else:
            counts = batch.request_counts
            total = (
                counts.processing
                + counts.succeeded
                + counts.errored
                + counts.canceled
                + counts.expired
            )
            logger.info(f"Batch {batch_id}: {counts.succeeded}/{total} complete")
            if batch.processing_status == "ended":
                return batch
        await asyncio.sleep(poll_interval_seconds)
    logger.error(f"batch {batch_id} did not end within {timeout_seconds}s")
    return None


async def get_batch_results(batch_id: str) -> dict[str, str | None]:
    """Stream batch results and map each custom_id to its response text.

    Non-succeeded results (errored, canceled, expired) and responses
    without a leading text block map to None so callers can record a
    per-article processing error — the same contract as ``call_claude``.
    """
    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    results: dict[str, str | None] = {}
    decoder = await client.messages.batches.results(batch_id)
    async for entry in decoder:
        results[entry.custom_id] = _batch_result_text(entry)
    return results


def _batch_result_text(entry: MessageBatchIndividualResponse) -> str | None:
    """Extract the text content from one batch result entry, or None."""
    if entry.result.type != "succeeded":
        logger.warning(f"batch request {entry.custom_id} ended as {entry.result.type}")
        return None
    content = entry.result.message.content
    if not content:
        logger.error(f"batch request {entry.custom_id} has no content blocks")
        return None
    block = content[0]
    if block.type != "text":
        logger.error(f"batch request {entry.custom_id} first block is {block.type}, not text")
        return None
    return block.text


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Parse ``text`` as a JSON object, salvaging embedded JSON if needed.

    Claude occasionally wraps the JSON in preamble or trailing text; the
    fallback slices from the first ``{`` to the last ``}`` and re-parses.
    Returns None when no JSON object can be recovered. dict[str, Any]:
    parsed Claude responses hold heterogeneous values.
    """
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            logger.error("response contains no JSON object")
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            logger.error(f"failed to parse response as JSON: {exc}")
            return None
    if not isinstance(parsed, dict):
        logger.error(f"response is not a JSON object: {type(parsed).__name__}")
        return None
    return parsed
