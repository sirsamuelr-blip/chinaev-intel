"""Signal detection: rule-based triggers + LLM narrative generation.

Implements the hybrid two-stage pipeline from ADR 003. Stage 1 runs
deterministic, pure trigger rules over a processed article's extracted
fields to produce signal candidates; Stage 2 sends each candidate to
Claude Sonnet for an editorial narrative (title, summary, implications for
Western OEMs, competitive impact score) and saves the result to the
``signals`` collection. This module is standalone — it is not wired into
the pipeline runner yet.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from db.firestore import get_all_features, save_signal
from processing.prompts import build_signal_narrative_message
from processing.utils import call_claude, parse_json_object

logger = logging.getLogger(__name__)

MAX_TOKENS = 1024

# Article docs carry no summary field (dropped at extraction time), so the
# narrative prompt falls back to a truncated bodyEn excerpt for context.
_BODY_EXCERPT_CHARS = 2000

SIGNAL_NEW_FEATURE_LAUNCH = "new_feature_launch"
SIGNAL_FEATURE_TRICKLE_DOWN = "feature_trickle_down"
SIGNAL_AI_INTEGRATION = "ai_integration"
SIGNAL_OTA_DEPLOYMENT = "ota_deployment"
SIGNAL_PARTNERSHIP_CHANGE = "partnership_change"
SIGNAL_CHIP_HARDWARE = "chip_hardware_announcement"

ALL_SIGNAL_TYPES = [
    SIGNAL_NEW_FEATURE_LAUNCH,
    SIGNAL_FEATURE_TRICKLE_DOWN,
    SIGNAL_AI_INTEGRATION,
    SIGNAL_OTA_DEPLOYMENT,
    SIGNAL_PARTNERSHIP_CHANGE,
    SIGNAL_CHIP_HARDWARE,
]

AI_PROVIDER_KEYWORDS = [
    "llm",
    "large language model",
    "gpt",
    "chatgpt",
    "ernie",
    "wenxin",
    "tongyi",
    "qianwen",
    "baidu ai",
    "sensetime",
    "deepseek",
    "large model",
    "foundation model",
    "generative ai",
]

CHIP_KEYWORDS = [
    "tops",
    "chip",
    "soc",
    "processor",
    "compute power",
    "computing power",
    "nvidia",
    "orin",
    "qualcomm",
    "snapdragon",
    "horizon robotics",
    "journey",
    "black sesame",
    "huawei ascend",
    "mdc",
]

NARRATIVE_REQUIRED_KEYS: frozenset[str] = frozenset(
    {"title", "summary", "implications_for_western_oems", "competitive_impact_score"}
)

# Article dicts use camelCase keys (Firestore doc shape) with snake_case keys
# inside each featuresExtracted item; existing-feature dicts from the db layer
# use snake_case. All hold heterogeneous values (str, bool, list, map, ...),
# so dict values are typed as Any throughout this module.


def _make_candidate(
    signal_type: str,
    article: dict[str, Any],
    features_mentioned: list[str],
    trigger_data: dict[str, Any],
) -> dict[str, Any]:
    """Build the common signal candidate shape shared by all triggers."""
    return {
        "signal_type": signal_type,
        "source_article_ids": [article["id"]],
        "brands_mentioned": article.get("brandsMentioned") or [],
        "features_mentioned": features_mentioned,
        "trigger_data": trigger_data,
    }


def _new_features(article: dict[str, Any]) -> list[dict[str, Any]]:
    """Return featuresExtracted items with ``is_new`` set, tolerating bad shapes."""
    features = article.get("featuresExtracted") or []
    return [feature for feature in features if isinstance(feature, dict) and feature.get("is_new")]


def check_new_feature_launch(article: dict[str, Any]) -> list[dict[str, Any]]:
    """Trigger: featuresExtracted contains any item where ``is_new`` is true.

    Returns one candidate per new feature; an empty list when the article
    has no new features.
    """
    return [
        _make_candidate(
            SIGNAL_NEW_FEATURE_LAUNCH,
            article,
            [str(feature.get("feature_name") or "")],
            {
                "feature_name": feature.get("feature_name"),
                "category": feature.get("category"),
                "description": feature.get("description"),
            },
        )
        for feature in _new_features(article)
    ]


def check_feature_trickle_down(
    article: dict[str, Any],
    existing_features: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Trigger: a new feature's category already exists for a different brand.

    ``existing_features`` comes from the features collection (snake_case
    keys). The match is deliberately loose — same category, different brand
    — and the Stage 2 competitive impact score separates real trickle-down
    from noise (ADR 003). Returns one candidate per (new feature, existing
    feature) match.
    """
    article_brands = {str(brand).lower() for brand in article.get("brandsMentioned") or []}
    candidates: list[dict[str, Any]] = []
    for feature in _new_features(article):
        category = feature.get("category")
        if not category:
            continue
        for existing in existing_features:
            existing_brand = str(existing.get("brand_name_en") or "")
            if existing.get("category") != category:
                continue
            if not existing_brand or existing_brand.lower() in article_brands:
                continue
            candidates.append(
                _make_candidate(
                    SIGNAL_FEATURE_TRICKLE_DOWN,
                    article,
                    [str(feature.get("feature_name") or "")],
                    {
                        "new_feature_name": feature.get("feature_name"),
                        "new_feature_category": category,
                        "existing_feature_name": existing.get("feature_name_en"),
                        "existing_brand": existing_brand,
                    },
                )
            )
    return candidates


def check_ai_integration(article: dict[str, Any]) -> list[dict[str, Any]]:
    """Trigger: a new ai_assistant feature, or AI-provider keywords in the text.

    The keyword branch only applies to ``news``/``opinion`` content and
    matches case-insensitively against titleEn and bodyEn. Returns at most
    one candidate per article, with the feature branch taking precedence.
    """
    features = article.get("featuresExtracted") or []
    ai_features = [
        feature
        for feature in features
        if isinstance(feature, dict) and feature.get("category") == "ai_assistant"
    ]
    ai_feature_names = [str(feature.get("feature_name") or "") for feature in ai_features]

    new_ai = [feature for feature in ai_features if feature.get("is_new")]
    if new_ai:
        trigger_data: dict[str, Any] = {
            "trigger_reason": "new_ai_feature",
            "matched_keywords": [],
            "feature_name": new_ai[0].get("feature_name"),
        }
        return [_make_candidate(SIGNAL_AI_INTEGRATION, article, ai_feature_names, trigger_data)]

    if article.get("contentType") not in ("news", "opinion"):
        return []
    text = f"{article.get('titleEn') or ''} {article.get('bodyEn') or ''}".lower()
    matched = [keyword for keyword in AI_PROVIDER_KEYWORDS if keyword in text]
    if not matched:
        return []
    trigger_data = {
        "trigger_reason": "ai_keyword_match",
        "matched_keywords": matched,
        "feature_name": None,
    }
    return [_make_candidate(SIGNAL_AI_INTEGRATION, article, ai_feature_names, trigger_data)]


def check_ota_deployment(article: dict[str, Any]) -> list[dict[str, Any]]:
    """Trigger: featuresExtracted contains a new ``ota`` feature.

    Returns one candidate per new OTA feature found.
    """
    return [
        _make_candidate(
            SIGNAL_OTA_DEPLOYMENT,
            article,
            [str(feature.get("feature_name") or "")],
            {
                "feature_name": feature.get("feature_name"),
                "description": feature.get("description"),
            },
        )
        for feature in _new_features(article)
        if feature.get("category") == "ota"
    ]


def check_partnership_change(article: dict[str, Any]) -> list[dict[str, Any]]:
    """Trigger: 2+ brands mentioned and a non-empty competitiveSignal.

    ``competitiveSignal`` is the Phase 1 extraction field stored on the
    article doc; a null or empty value never fires. Returns at most one
    candidate per article.
    """
    brands = article.get("brandsMentioned") or []
    competitive_signal = article.get("competitiveSignal")
    if len(brands) < 2 or not competitive_signal:
        return []
    trigger_data = {"competitive_signal": competitive_signal, "brand_count": len(brands)}
    return [_make_candidate(SIGNAL_PARTNERSHIP_CHANGE, article, [], trigger_data)]


def check_chip_hardware_announcement(article: dict[str, Any]) -> list[dict[str, Any]]:
    """Trigger: an ``adas`` feature whose description mentions chip keywords.

    Keyword matching is a case-insensitive substring check against the
    feature description. Returns one candidate per matching feature.
    """
    features = article.get("featuresExtracted") or []
    candidates: list[dict[str, Any]] = []
    for feature in features:
        if not isinstance(feature, dict) or feature.get("category") != "adas":
            continue
        description = str(feature.get("description") or "")
        matched = [keyword for keyword in CHIP_KEYWORDS if keyword in description.lower()]
        if not matched:
            continue
        candidates.append(
            _make_candidate(
                SIGNAL_CHIP_HARDWARE,
                article,
                [str(feature.get("feature_name") or "")],
                {
                    "feature_name": feature.get("feature_name"),
                    "matched_keywords": matched,
                    "description": feature.get("description"),
                },
            )
        )
    return candidates


async def detect_signal_candidates(
    article: dict[str, Any],
    existing_features: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Run all trigger rules against one article and combine the candidates.

    ``existing_features`` feeds the trickle-down check; when None that
    check is skipped with a debug log. A single trigger failure is logged
    and never prevents the other triggers from running.
    """
    article_id = article.get("id")
    checks: list[tuple[str, Callable[[], list[dict[str, Any]]]]] = [
        (SIGNAL_NEW_FEATURE_LAUNCH, lambda: check_new_feature_launch(article))
    ]
    if existing_features is None:
        logger.debug(
            f"no existing_features provided, skipping trickle-down check article_id={article_id}"
        )
    else:
        features = existing_features
        checks.append(
            (SIGNAL_FEATURE_TRICKLE_DOWN, lambda: check_feature_trickle_down(article, features))
        )
    checks.extend(
        [
            (SIGNAL_AI_INTEGRATION, lambda: check_ai_integration(article)),
            (SIGNAL_OTA_DEPLOYMENT, lambda: check_ota_deployment(article)),
            (SIGNAL_PARTNERSHIP_CHANGE, lambda: check_partnership_change(article)),
            (SIGNAL_CHIP_HARDWARE, lambda: check_chip_hardware_announcement(article)),
        ]
    )

    candidates: list[dict[str, Any]] = []
    for signal_type, check in checks:
        try:
            candidates.extend(check())
        except Exception:
            logger.exception(f"trigger failed type={signal_type} article_id={article_id}")
    return candidates


def _parse_narrative(text: str) -> dict[str, Any] | None:
    """Parse Claude's narrative response as JSON and validate the narrative keys.

    Brace-salvage parsing is delegated to
    ``processing.utils.parse_json_object``; this adds the
    narrative-specific required-key validation.
    """
    parsed = parse_json_object(text)
    if parsed is None:
        return None
    missing = NARRATIVE_REQUIRED_KEYS - parsed.keys()
    if missing:
        logger.error(f"narrative response missing required keys: {sorted(missing)}")
        return None
    return parsed


async def generate_signal_narrative(
    candidate: dict[str, Any],
    article: dict[str, Any],
) -> dict[str, Any] | None:
    """Call Sonnet to generate the editorial narrative for one candidate.

    On success the narrative fields (title, summary,
    implications_for_western_oems, competitive_impact_score) are merged
    into a copy of the candidate. Returns None on any failure — API errors
    after retries or malformed/incomplete JSON. Never raises.
    """
    messages = build_signal_narrative_message(
        signal_type=str(candidate.get("signal_type") or ""),
        headline=str(article.get("titleEn") or article.get("title") or ""),
        summary=str(article.get("summary") or article.get("bodyEn") or "")[:_BODY_EXCERPT_CHARS],
        brands=[str(brand) for brand in candidate.get("brands_mentioned") or []],
        features=[str(feature) for feature in candidate.get("features_mentioned") or []],
        trigger_data=candidate.get("trigger_data") or {},
    )
    text = await call_claude(messages, max_tokens=MAX_TOKENS)
    if text is None:
        return None
    narrative = _parse_narrative(text)
    if narrative is None:
        return None
    return {**candidate, **narrative}


def _to_signal_data(signal: dict[str, Any]) -> dict[str, Any]:
    """Map a merged candidate + narrative dict to the ``save_signal`` input shape.

    ``trigger_data`` is intentionally dropped — the signals schema has no
    such field; the detection context lives only in the prompt.
    """
    return {
        "signal_type": signal["signal_type"],
        "title": signal["title"],
        "summary": signal["summary"],
        "brands_mentioned": signal["brands_mentioned"],
        "features_mentioned": signal["features_mentioned"],
        "source_article_ids": signal["source_article_ids"],
        "implications_for_western_oems": signal["implications_for_western_oems"],
        "competitive_impact_score": signal["competitive_impact_score"],
    }


async def detect_signals_from_articles(articles: list[dict[str, Any]]) -> dict[str, Any]:
    """Detect and persist signals for a batch of processed article docs.

    Skips duplicate articles (ADR 004), runs the Stage 1 triggers, sends
    each candidate through Stage 2 narrative generation, and saves
    successful signals to Firestore. Existing features are fetched once up
    front for the trickle-down check; if that fetch fails the run
    continues without it. A single article, candidate, or Firestore
    failure never crashes the batch. Returns a run summary.
    """
    existing_features: list[dict[str, Any]] | None = None
    try:
        existing_features = await get_all_features()
    except Exception:
        logger.exception("failed to fetch existing features, skipping trickle-down checks")

    summary: dict[str, Any] = {
        "articles_processed": 0,
        "articles_skipped_duplicate": 0,
        "candidates_detected": 0,
        "signals_generated": 0,
        "signals_failed": 0,
        "signals_by_type": dict.fromkeys(ALL_SIGNAL_TYPES, 0),
    }
    for article in articles:
        try:
            if article.get("isDuplicate"):
                summary["articles_skipped_duplicate"] += 1
                continue
            summary["articles_processed"] += 1
            candidates = await detect_signal_candidates(article, existing_features)
            summary["candidates_detected"] += len(candidates)
            for candidate in candidates:
                signal = await generate_signal_narrative(candidate, article)
                if signal is None:
                    summary["signals_failed"] += 1
                    continue
                try:
                    await save_signal(_to_signal_data(signal))
                except Exception:
                    logger.exception(
                        f"failed to save signal type={signal.get('signal_type')} "
                        f"article_id={article.get('id')}"
                    )
                    summary["signals_failed"] += 1
                    continue
                summary["signals_generated"] += 1
                signal_type = str(signal.get("signal_type"))
                if signal_type in summary["signals_by_type"]:
                    summary["signals_by_type"][signal_type] += 1
        except Exception:
            logger.exception(f"signal detection failed article_id={article.get('id')}")
    logger.info(f"signal detection complete summary={summary}")
    return summary
