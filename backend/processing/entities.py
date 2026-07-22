"""Entity promotion: resolves and upserts brands, vehicles, features from processed articles.

Implements the hybrid entity resolution strategy from ADR 005: brand names
are resolved against a curated alias dictionary first, with a Claude Sonnet
fallback for names the dictionary does not know. Resolved entities are
upserted into the ``brands``, ``vehicles``, and ``features`` collections.
Wired into the pipeline runner as a Phase 2 step (see
``scrapers/runner.py::_phase2_promote_entities``).

The Sonnet fallback is driven through a per-run ``BrandResolver``: each
distinct brand-name string is resolved at most once, the number of Sonnet
calls is capped per run (``MAX_SONNET_BRAND_RESOLUTIONS``), and known
non-automotive false positives (``NON_AUTOMOTIVE_BRANDS``) are filtered out
before any resolution attempt. Together these keep runaway API cost in check.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from db.firestore import (
    get_brand_by_name_en,
    upsert_brand,
    upsert_feature,
    upsert_vehicle,
)
from processing.prompts import build_brand_resolution_message
from processing.utils import call_claude

logger = logging.getLogger(__name__)

ALIASES_PATH = Path(__file__).parent.parent / "config" / "brand_aliases.json"

MAX_TOKENS = 1024

# Maximum Sonnet brand-resolution fallback calls allowed in a single pipeline
# run. Dictionary hits are free and uncounted; only the LLM fallback counts
# against this, so one runaway run cannot rack up unbounded API cost.
MAX_SONNET_BRAND_RESOLUTIONS = 20

# Vehicle names like "Lynk & Co 08" carry up to three brand words before the model.
_MAX_BRAND_PREFIX_WORDS = 3

# Entities the LLM extraction occasionally returns as brands that are not
# automakers at all — streaming platforms, phone makers, phone OSes, venues.
# They are filtered before resolution so they never trigger a Sonnet fallback
# call or a "skipping unresolvable brand" warning.
NON_AUTOMOTIVE_BRANDS: frozenset[str] = frozenset(
    {
        "iQIYI",
        "爱奇艺",
        "Honor",
        "荣耀",
        "Flyme",
        "元镜",
        "Yuan Mirror",
        "国家大剧院",
        "National Centre for the Performing Arts",
    }
)
_NON_AUTOMOTIVE_LOWERED: frozenset[str] = frozenset(name.lower() for name in NON_AUTOMOTIVE_BRANDS)

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


def is_non_automotive_brand(name: str) -> bool:
    """Return True when ``name`` is a known non-automotive false positive.

    Matching is case-insensitive (a no-op for the Chinese entries). Callers
    use this to drop such names before any resolution attempt, so they never
    reach the alias dictionary or the Sonnet fallback.
    """
    return name.strip().lower() in _NON_AUTOMOTIVE_LOWERED


def _salvage_json(text: str) -> object:
    """Parse ``text`` as JSON, retrying on the first-``{`` to last-``}`` slice.

    Sonnet occasionally appends commentary after the JSON object; the retry
    salvages the object from that surrounding text. Mirrors
    ``utils.parse_json_object``'s brace salvage but stays local so the caller
    owns logging (WARNING with the brand name) and can treat a bare ``null``
    as the expected "not a known brand" answer rather than an error. Raises
    ``json.JSONDecodeError`` when neither parse succeeds.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def _parse_resolution(text: str, name: str) -> dict[str, Any] | None:
    """Parse the brand resolution response from Claude for ``name``.

    Returns the resolved mapping dict, or None when Claude answered ``null``
    (not a known brand) or returned something unparseable. A parse failure is
    logged at WARNING with the brand name and a truncated response so bad
    responses are diagnosable without leaking the full payload into logs.
    """
    stripped = text.strip()
    try:
        parsed = _salvage_json(stripped)
    except json.JSONDecodeError:
        logger.warning(
            f"brand resolution response not valid JSON name={name} response={stripped[:200]!r}"
        )
        return None
    if parsed is None:
        return None  # Sonnet's expected "not a known brand" answer.
    if not isinstance(parsed, dict) or not parsed.get("name_en"):
        logger.warning(
            f"brand resolution response missing name_en name={name} response={stripped[:200]!r}"
        )
        return None
    return parsed


async def _resolve_brand_via_sonnet(name: str) -> str | None:
    """Resolve one dictionary-unknown brand name via a single Sonnet call.

    Returns the canonical English name, or None when Claude answers ``null``
    (not a known brand), the call fails, or the response cannot be parsed. A
    successful resolution is logged for human review and addition to
    ``brand_aliases.json`` — it is never written back automatically.
    """
    text = await call_claude(
        [{"role": "user", "content": build_brand_resolution_message(name)}],
        max_tokens=MAX_TOKENS,
    )
    if text is None:
        logger.warning(f"llm brand resolution failed name={name}")
        return None
    resolved = _parse_resolution(text, name)
    if resolved is None:
        logger.info(f"llm brand resolution: not a known brand name={name}")
        return None
    logger.info(
        "llm resolved new brand mapping (review and add to brand_aliases.json): "
        f"alias={name} name_en={resolved['name_en']} name_zh={resolved.get('name_zh')} "
        f"parent_group={resolved.get('parent_group')}"
    )
    return str(resolved["name_en"])


class BrandResolver:
    """Per-run brand resolver: alias dictionary first, then capped Sonnet fallback.

    Holds run-scoped state so a single pipeline run resolves each distinct
    dictionary-unknown name at most once (memoized, successes and failures
    alike) and makes at most ``MAX_SONNET_BRAND_RESOLUTIONS`` Sonnet calls in
    total. Construct one per run and share it across every article in that run
    to prevent the same unknown brand from triggering repeated LLM calls.
    """

    def __init__(self, aliases: dict[str, Any]) -> None:
        self.aliases = aliases
        # name (stripped, lowercased) -> resolved canonical name or None.
        self._cache: dict[str, str | None] = {}
        self._sonnet_calls = 0
        self._cap_logged = False

    async def resolve(self, name: str) -> str | None:
        """Resolve ``name`` to a canonical brand, dictionary first then Sonnet.

        The dictionary path is free and always tried. The Sonnet fallback is
        memoized for the run and capped; once the cap is hit, remaining
        unknown names resolve to None after a single log line.
        """
        canonical = resolve_brand_name(name, self.aliases)
        if canonical is not None:
            return canonical
        key = name.strip().lower()
        if key in self._cache:
            return self._cache[key]
        if self._sonnet_calls >= MAX_SONNET_BRAND_RESOLUTIONS:
            if not self._cap_logged:
                logger.info("brand resolution cap reached, skipping remaining unresolved brands")
                self._cap_logged = True
            return None
        self._sonnet_calls += 1
        resolved = await _resolve_brand_via_sonnet(name)
        self._cache[key] = resolved
        return resolved


async def resolve_brand_with_fallback(
    name: str, aliases: dict[str, Any], resolver: BrandResolver | None = None
) -> str | None:
    """Resolve a brand name via the alias dictionary, falling back to Sonnet.

    Delegates to a ``BrandResolver``: pass a shared one to reuse a run's
    resolution cache and Sonnet-call budget, or omit it for a one-off
    resolution with a fresh per-call resolver. Returns None when neither the
    dictionary nor the fallback resolves the name.
    """
    if resolver is None:
        resolver = BrandResolver(aliases)
    return await resolver.resolve(name)


async def promote_brands(
    brand_names: list[str],
    aliases: dict[str, Any],
    resolver: BrandResolver | None = None,
) -> dict[str, str]:
    """Resolve each brand name and upsert it into the brands collection.

    Returns a mapping of original name -> brand doc ID. Known non-automotive
    false positives are dropped silently before any resolution attempt.
    Unresolvable names are skipped with a warning; a single failure never
    aborts the remaining names. Brands resolved by the LLM fallback have no
    dictionary metadata and are upserted with ``name_en`` only. Pass a shared
    ``resolver`` to reuse the run's resolution cache and Sonnet-call budget.
    """
    if resolver is None:
        resolver = BrandResolver(aliases)
    brand_map: dict[str, str] = {}
    for name in brand_names:
        if is_non_automotive_brand(name):
            continue  # known false positive: skip silently, no Sonnet call
        try:
            canonical = await resolver.resolve(name)
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


async def promote_entities_from_article(
    article_doc: dict[str, Any], resolver: BrandResolver | None = None
) -> dict[str, int]:
    """Promote all extracted entities from one processed article doc.

    ``article_doc`` uses the Firestore doc shape: camelCase top-level keys
    (``brandsMentioned``, ``vehiclesMentioned``, ``featuresExtracted``) with
    snake_case keys inside each extracted feature, as written by the LLM
    pipeline. Missing or empty fields skip that promotion step. Returns a
    summary of promoted entity counts.

    Pass a shared ``resolver`` to share the brand-resolution cache and
    Sonnet-call budget across every article in a run; when omitted, a fresh
    per-article resolver is used.
    """
    article_id = article_doc.get("id")
    if resolver is None:
        resolver = BrandResolver(load_aliases())
    aliases = resolver.aliases

    brand_names = article_doc.get("brandsMentioned") or []
    if not brand_names:
        logger.warning(f"article has no brandsMentioned, skipping brands article_id={article_id}")
    brand_map = await promote_brands(brand_names, aliases, resolver)

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
