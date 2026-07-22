"""Prompt templates for the LLM processing modules.

``EXTRACTION_PROMPT`` is copied verbatim from docs/llm-pipeline.md — do not
edit it here without updating the spec. It is sent as a cached system prompt
(see ``utils.call_claude``); ``build_extraction_message`` formats a single
article's title and body as the accompanying user message.
``BRAND_RESOLUTION_PROMPT`` backs the entity promotion fallback (ADR 005):
it asks Sonnet to resolve a brand name the alias dictionary does not know.
``SIGNAL_NARRATIVE_PROMPT`` backs Stage 2 of signal detection (ADR 003): it
asks Sonnet to generate an editorial narrative for a signal candidate.
"""

from __future__ import annotations

import json
from typing import Any

from anthropic.types import MessageParam

EXTRACTION_PROMPT: str = """You are an automotive intelligence analyst. Given this article about the
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

Respond in JSON only. No markdown fences. No preamble."""


BRAND_RESOLUTION_PROMPT: str = """You are an automotive intelligence analyst. Determine whether the
following name refers to a known Chinese EV brand, automaker, or automotive
supplier.

If it does, respond with a JSON object:
{"name_en": "<canonical English brand name>",
 "name_zh": "<Chinese brand name or null>",
 "parent_group": "<parent company name or null>"}

If it does not, respond with exactly: null

Respond in JSON only. No markdown fences. No preamble."""


SIGNAL_NARRATIVE_PROMPT: str = """\
You are an automotive competitive intelligence analyst. Given a signal
detected from Chinese EV industry coverage, generate an editorial-quality intelligence brief entry.

Signal type: {signal_type}
Article headline: {headline}
Article summary: {summary}
Brands involved: {brands}
Features involved: {features}
Detection context: {trigger_data}

Generate the following fields:

1. title: A concise, specific one-line headline for this signal. Include brand names and
   feature names where relevant. Not generic — a reader should understand the signal from
   the title alone.

2. summary: 2-3 sentences explaining what happened and why it matters. Be specific about
   what was announced, launched, or changed.

3. implications_for_western_oems: 1-2 sentences on why this matters for Western automakers
   (GM, Ford, VW, Toyota, Stellantis, BMW, Hyundai) and/or Western Tier 1 suppliers
   (Bosch, Continental, ZF). Be specific about the competitive implication, not generic
   "they should pay attention."

4. competitive_impact_score: Integer 1-10. Use this scale:
   - 1-3: Minor or incremental (routine update, small feature addition)
   - 4-6: Noteworthy (meaningful feature launch, new partnership, cost milestone)
   - 7-8: Significant (industry-first capability, major strategic shift, pricing disruption)
   - 9-10: Critical (paradigm shift, regulatory mandate, fundamental competitive threat)

Respond in JSON only. No markdown fences. No preamble."""


def build_extraction_message(title: str, body: str) -> str:
    """Format one article's title and body as the extraction user message.

    The extraction prompt itself is not included here — the pipeline sends
    ``EXTRACTION_PROMPT`` as a cached system prompt so its tokens are
    shared across sequential article calls.
    """
    return f"Title: {title}\n{body}"


def build_brand_resolution_message(name: str) -> str:
    """Combine the brand resolution prompt with the name to resolve."""
    return f"{BRAND_RESOLUTION_PROMPT}\n\nName: {name}"


def build_signal_narrative_message(
    signal_type: str,
    headline: str,
    summary: str,
    brands: list[str],
    features: list[str],
    trigger_data: dict[str, Any],
) -> list[MessageParam]:
    """Build the Claude API messages list for signal narrative generation.

    ``trigger_data`` is rendered as indented JSON so the model sees the
    detection context in a readable form. The return value is
    ``[{"role": "user", "content": <formatted prompt>}]``, typed as
    ``list[MessageParam]`` to satisfy the anthropic SDK under mypy strict.
    """
    prompt = SIGNAL_NARRATIVE_PROMPT.format(
        signal_type=signal_type,
        headline=headline,
        summary=summary,
        brands=", ".join(brands),
        features=", ".join(features),
        trigger_data=json.dumps(trigger_data, indent=2, ensure_ascii=False),
    )
    return [{"role": "user", "content": prompt}]
