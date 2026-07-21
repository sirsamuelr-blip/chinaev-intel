"""Prompt templates for the LLM processing modules.

``EXTRACTION_PROMPT`` is copied verbatim from docs/llm-pipeline.md — do not
edit it here without updating the spec. ``build_extraction_message`` combines
the prompt with a single article's title and body for the Claude API call.
``BRAND_RESOLUTION_PROMPT`` backs the entity promotion fallback (ADR 005):
it asks Sonnet to resolve a brand name the alias dictionary does not know.
"""

from __future__ import annotations

EXTRACTION_PROMPT: str = """You are an automotive intelligence analyst. Given this article about the
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


def build_extraction_message(title: str, body: str) -> str:
    """Combine the extraction prompt with one article's title and body."""
    return f"{EXTRACTION_PROMPT}\n\nTitle: {title}\n{body}"


def build_brand_resolution_message(name: str) -> str:
    """Combine the brand resolution prompt with the name to resolve."""
    return f"{BRAND_RESOLUTION_PROMPT}\n\nName: {name}"
