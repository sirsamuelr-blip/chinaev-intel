"""Shared helpers for the LLM processing modules.

``call_claude`` is the single Claude API retry helper used by the
extraction pipeline, entity promotion, and signal detection — extracted
from the per-module copies tracked in docs/tech-debt.md.
``parse_json_object`` is the shared brace-salvage JSON parser for
Claude responses that wrap JSON in preamble or trailing text.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import anthropic
from anthropic.types import MessageParam

from config import settings

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 4096


async def call_claude(
    messages: list[MessageParam],
    model: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str | None:
    """Call the Claude API with retries and return the response text.

    ``model`` defaults to ``settings.SONNET_MODEL`` when None. Retries
    with exponential backoff on any anthropic exception, up to
    ``settings.MAX_RETRIES`` retries after the initial attempt. Returns
    None once all attempts are exhausted or the response has no text
    content.
    """
    resolved_model = model if model is not None else settings.SONNET_MODEL
    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    for attempt in range(settings.MAX_RETRIES + 1):
        try:
            response = await client.messages.create(
                model=resolved_model,
                max_tokens=max_tokens,
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
