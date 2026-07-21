"""Entity promotion: resolves and upserts brands, vehicles, features from processed articles.

Implements the hybrid entity resolution strategy from ADR 005: brand names
are resolved against a curated alias dictionary first, with a Claude Sonnet
fallback for names the dictionary does not know. Resolved entities are
upserted into the ``brands``, ``vehicles``, and ``features`` collections.
This module is standalone — it is not wired into the pipeline runner yet.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import anthropic

from config import settings
from db.firestore import (
    get_brand_by_name_en,
    upsert_brand,
    upsert_feature,
    upsert_vehicle,
)
from processing.prompts import build_brand_resolution_message

logger = logging.getLogger(__name__)

ALIASES_PATH = Path(__file__).parent.parent / "config" / "brand_aliases.json"

MAX_TOKENS = 1024

# Vehicle names like "Lynk & Co 08" carry up to three brand words before the model.
_MAX_BRAND_PREFIX_WORDS = 3

# Alias/article/Firestore dicts hold heterogeneous values (str, bool, list,
# map, ...), so dict values are typed as Any throughout this module.
_aliases_cache: dict[str, Any] | None = None


def load_aliases() -> dict[str, Any]:
    """Load the brand alias dictionary, caching it after the first read.

    Returns the full dict with ``aliases`` (variant name -> canonical
    English name) and ``brands`` (canonical name -> metadata) keys.
    """
    global _aliases_cache
    if _aliases_cache is None:
        _aliases_cache = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
    return _aliases_cache


def resolve_brand_name(name: str, aliases: dict[str, Any]) -> str | None:
    """Resolve a brand name to its canonical English form via the alias map.

    Lookup is case-insensitive (a no-op for Chinese names, which match
    exactly). Returns None when the name is not in the dictionary.
    """
    lowered: dict[str, str] = {
        alias.lower(): canonical for alias, canonical in aliases["aliases"].items()
    }
    return lowered.get(name.strip().lower())


async def _call_claude(client: anthropic.AsyncAnthropic, message: str) -> str | None:
    """Call the Claude API with retries and return the response text.

    Mirrors the retry pattern in ``processing.pipeline``: exponential backoff
    on any anthropic exception, up to ``settings.MAX_RETRIES`` retries after
    the initial attempt. Returns None once all attempts are exhausted or the
    response has no text content.
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


def _parse_resolution(text: str) -> dict[str, Any] | None:
    """Parse the brand resolution response from Claude.

    Returns the resolved mapping dict, or None when Claude answered ``null``
    (not a known brand) or returned something unparseable.
    """
    try:
        parsed = json.loads(text.strip())
    except json.JSONDecodeError:
        logger.error(f"brand resolution response is not valid JSON: {text[:200]!r}")
        return None
    if not isinstance(parsed, dict):
        return None
    if not parsed.get("name_en"):
        logger.error("brand resolution response missing name_en")
        return None
    return parsed


async def resolve_brand_with_fallback(name: str, aliases: dict[str, Any]) -> str | None:
    """Resolve a brand name via the alias dictionary, falling back to Sonnet.

    On a Sonnet resolution the mapping is logged for human review and
    addition to ``brand_aliases.json`` — it is never written automatically.
    Returns None when neither path resolves the name.
    """
    canonical = resolve_brand_name(name, aliases)
    if canonical is not None:
        return canonical
    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    text = await _call_claude(client, build_brand_resolution_message(name))
    if text is None:
        logger.warning(f"llm brand resolution failed name={name}")
        return None
    resolved = _parse_resolution(text)
    if resolved is None:
        logger.info(f"llm brand resolution: not a known brand name={name}")
        return None
    logger.info(
        "llm resolved new brand mapping (review and add to brand_aliases.json): "
        f"alias={name} name_en={resolved['name_en']} name_zh={resolved.get('name_zh')} "
        f"parent_group={resolved.get('parent_group')}"
    )
    return str(resolved["name_en"])


async def promote_brands(brand_names: list[str], aliases: dict[str, Any]) -> dict[str, str]:
    """Resolve each brand name and upsert it into the brands collection.

    Returns a mapping of original name -> brand doc ID. Unresolvable names
    are skipped; a single failure never aborts the remaining names. Brands
    resolved by the LLM fallback have no dictionary metadata and are
    upserted with ``name_en`` only.
    """
    brand_map: dict[str, str] = {}
    for name in brand_names:
        try:
            canonical = await resolve_brand_with_fallback(name, aliases)
            if canonical is None:
                logger.warning(f"skipping unresolvable brand name={name}")
                continue
            brand_data: dict[str, Any] = {"name_en": canonical}
            metadata = aliases["brands"].get(canonical)
            if metadata is not None:
                brand_data["name_zh"] = metadata["nameZh"]
                brand_data["parent_group"] = metadata["parentGroup"]
                brand_data["ev_focus"] = metadata["evFocus"]
            brand_map[name] = await upsert_brand(brand_data)
        except Exception:
            logger.exception(f"brand promotion failed name={name}")
    return brand_map


def _split_vehicle_name(name: str, aliases: dict[str, Any]) -> tuple[str, str] | None:
    """Split a vehicle name like ``"BYD Seal"`` into (canonical brand, model).

    Tries word prefixes longest-first against the alias dictionary so
    multi-word brands ("Li Auto L9", "Lynk & Co 08") resolve correctly. At
    least one word must remain as the model name. Returns None when no
    prefix resolves to a known brand.
    """
    words = name.strip().split()
    for count in range(min(len(words) - 1, _MAX_BRAND_PREFIX_WORDS), 0, -1):
        canonical = resolve_brand_name(" ".join(words[:count]), aliases)
        if canonical is not None:
            return canonical, " ".join(words[count:])
    return None


async def promote_vehicles(
    vehicle_names: list[str],
    brand_map: dict[str, str],
    brand_aliases: dict[str, Any],
) -> dict[str, str]:
    """Parse vehicle names into brand + model and upsert into vehicles.

    Returns a mapping of original vehicle name -> vehicle doc ID. The brand
    doc ID comes from ``brand_map`` (original brand name -> doc ID) when the
    canonical brand matches, falling back to a Firestore lookup. Vehicles
    whose brand cannot be resolved are logged and skipped.
    """
    canonical_ids: dict[str, str] = {}
    for original, doc_id in brand_map.items():
        canonical = resolve_brand_name(original, brand_aliases)
        if canonical is not None:
            canonical_ids[canonical] = doc_id

    vehicle_map: dict[str, str] = {}
    for name in vehicle_names:
        try:
            parsed = _split_vehicle_name(name, brand_aliases)
            if parsed is None:
                logger.warning(f"skipping vehicle with unresolvable brand name={name}")
                continue
            canonical_brand, model = parsed
            brand_id = canonical_ids.get(canonical_brand)
            if brand_id is None:
                existing = await get_brand_by_name_en(canonical_brand)
                if existing is None:
                    logger.warning(f"skipping vehicle, brand not in firestore name={name}")
                    continue
                brand_id = str(existing["id"])
                canonical_ids[canonical_brand] = brand_id
            vehicle_map[name] = await upsert_vehicle(
                {
                    "brand_id": brand_id,
                    "brand_name_en": canonical_brand,
                    "model_name_en": model,
                }
            )
        except Exception:
            logger.exception(f"vehicle promotion failed name={name}")
    return vehicle_map


async def promote_features(
    features_extracted: list[dict[str, Any]],
    brand_map: dict[str, str],
    vehicle_map: dict[str, str],
) -> list[str]:
    """Upsert new features (``is_new == True``) into the features collection.

    Returns the list of created/updated feature doc IDs. Each feature links
    to the article's first resolved brand (required — with no resolved brand
    all features are skipped) and first resolved vehicle (optional).
    ``firstSeenDate`` handling lives in the Firestore layer, which sets it
    on new docs only.
    """
    new_features = [feature for feature in features_extracted if feature.get("is_new")]
    if not new_features:
        return []
    if not brand_map:
        logger.warning("skipping feature promotion: no resolved brand to link")
        return []

    aliases = load_aliases()
    first_brand, brand_id = next(iter(brand_map.items()))
    brand_name_en = resolve_brand_name(first_brand, aliases) or first_brand
    vehicle_id: str | None = None
    vehicle_model_name: str | None = None
    if vehicle_map:
        first_vehicle, vehicle_id = next(iter(vehicle_map.items()))
        parsed = _split_vehicle_name(first_vehicle, aliases)
        vehicle_model_name = parsed[1] if parsed is not None else first_vehicle

    feature_ids: list[str] = []
    for feature in new_features:
        try:
            feature_data: dict[str, Any] = {
                "brand_id": brand_id,
                "brand_name_en": brand_name_en,
                "feature_name_en": feature["feature_name"],
                "category": feature["category"],
                "description": feature.get("description", ""),
                "supplier": feature.get("supplier"),
                "launch_type": "new",
            }
            if vehicle_id is not None:
                feature_data["vehicle_id"] = vehicle_id
                feature_data["vehicle_model_name"] = vehicle_model_name
            feature_ids.append(await upsert_feature(feature_data))
        except Exception:
            logger.exception(f"feature promotion failed name={feature.get('feature_name')}")
    return feature_ids


async def promote_entities_from_article(article_doc: dict[str, Any]) -> dict[str, int]:
    """Promote all extracted entities from one processed article doc.

    ``article_doc`` uses the Firestore doc shape: camelCase top-level keys
    (``brandsMentioned``, ``vehiclesMentioned``, ``featuresExtracted``) with
    snake_case keys inside each extracted feature, as written by the LLM
    pipeline. Missing or empty fields skip that promotion step. Returns a
    summary of promoted entity counts.
    """
    article_id = article_doc.get("id")
    aliases = load_aliases()

    brand_names = article_doc.get("brandsMentioned") or []
    if not brand_names:
        logger.warning(f"article has no brandsMentioned, skipping brands article_id={article_id}")
    brand_map = await promote_brands(brand_names, aliases)

    vehicle_names = article_doc.get("vehiclesMentioned") or []
    if not vehicle_names:
        logger.info(f"article has no vehiclesMentioned, skipping vehicles article_id={article_id}")
    vehicle_map = await promote_vehicles(vehicle_names, brand_map, aliases)

    features_extracted = article_doc.get("featuresExtracted") or []
    if not features_extracted:
        logger.info(f"article has no featuresExtracted, skipping features article_id={article_id}")
    feature_ids = await promote_features(features_extracted, brand_map, vehicle_map)

    summary = {
        "brands_promoted": len(brand_map),
        "vehicles_promoted": len(vehicle_map),
        "features_promoted": len(feature_ids),
    }
    logger.info(f"entity promotion complete article_id={article_id} summary={summary}")
    return summary
