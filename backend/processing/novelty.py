"""Novelty scoring for articles and signals against recent coverage.

Answers "is this telling us something new, or recycling ground we already
tracked?" Deduplication (ADR 004) catches the *same* story reported by
multiple sources; novelty scoring catches *rehashed* content — coverage
overlapping what recent articles or signals already established. Scores
run 0.0-1.0 (1.0 = fully novel) and are the inverse of the maximum
weighted similarity against any recent item. Analysis only — novelty
scores are never written to Firestore; persistence is the pipeline
integration's job (Task 9). This module is standalone — it is not wired
into the pipeline runner yet.
"""

from __future__ import annotations

import logging
from typing import Any

from db.firestore import _keys_to_camel, get_recent_processed_articles, get_recent_signals
from processing.dedup import extract_feature_categories, jaccard_similarity, title_similarity

logger = logging.getLogger(__name__)

ARTICLE_TITLE_WEIGHT = 0.35
ARTICLE_BRAND_WEIGHT = 0.25
ARTICLE_CONTENT_WEIGHT = 0.25
ARTICLE_TYPE_WEIGHT = 0.15
SIGNAL_TYPE_WEIGHT = 0.3
SIGNAL_BRAND_WEIGHT = 0.35
SIGNAL_FEATURE_WEIGHT = 0.2
SIGNAL_SOURCE_WEIGHT = 0.15
DEFAULT_ARTICLE_LOOKBACK_DAYS = 7
DEFAULT_SIGNAL_LOOKBACK_DAYS = 14

# Article dicts use camelCase keys (Firestore doc shape); signal dicts use
# snake_case keys (db layer convention). Both hold heterogeneous values
# (str, int, list, map, ...), so dict values are typed as Any throughout
# this module.


def _primary_brand(article: dict[str, Any]) -> str | None:
    """Return the first brandsMentioned entry lowercased, or None when absent."""
    brands = article.get("brandsMentioned") or []
    if not brands:
        return None
    return str(brands[0]).lower()


def _content_overlap_set(article: dict[str, Any]) -> set[str]:
    """Combine vehiclesMentioned and feature categories into one overlap set."""
    vehicles = {str(vehicle) for vehicle in article.get("vehiclesMentioned") or []}
    return vehicles | extract_feature_categories(article.get("featuresExtracted"))


def _article_similarity(article: dict[str, Any], other: dict[str, Any]) -> float:
    """Weighted similarity between two articles for novelty purposes.

    Unlike dedup's ``compute_similarity`` there is no publish-date gate —
    recency is already bounded by the caller's lookback fetch. The
    content-type dimension is a binary penalty: another piece of the same
    contentType about the same primary brand means the new one likely
    rehashes known coverage.
    """
    title_score = title_similarity(article.get("titleEn"), other.get("titleEn"))
    brand_score = jaccard_similarity(
        set(article.get("brandsMentioned") or []),
        set(other.get("brandsMentioned") or []),
    )
    content_score = jaccard_similarity(_content_overlap_set(article), _content_overlap_set(other))
    same_type = bool(article.get("contentType")) and article.get("contentType") == other.get(
        "contentType"
    )
    primary = _primary_brand(article)
    same_primary_brand = primary is not None and primary == _primary_brand(other)
    return (
        title_score * ARTICLE_TITLE_WEIGHT
        + brand_score * ARTICLE_BRAND_WEIGHT
        + content_score * ARTICLE_CONTENT_WEIGHT
        + (ARTICLE_TYPE_WEIGHT if same_type and same_primary_brand else 0.0)
    )


def score_article_novelty(article: dict[str, Any], recent_articles: list[dict[str, Any]]) -> float:
    """Score how novel an article is against recent coverage (camelCase keys).

    Compares against every recent article on title similarity, brand
    overlap, vehicle + feature-category overlap, and a same-content-type /
    same-primary-brand penalty. Novelty is 1.0 minus the maximum
    similarity found, clamped to [0.0, 1.0]. The article itself and
    members of its duplicate group are skipped — same-story matches are
    dedup's territory (ADR 004), not recycled coverage. No recent
    articles means fully novel (1.0).
    """
    max_similarity = 0.0
    group_id = article.get("duplicateGroupId")
    for recent in recent_articles:
        if recent.get("id") == article.get("id"):
            continue
        if group_id and recent.get("duplicateGroupId") == group_id:
            continue
        max_similarity = max(max_similarity, _article_similarity(article, recent))
    return min(1.0, max(0.0, 1.0 - max_similarity))


def _signal_similarity(signal: dict[str, Any], other: dict[str, Any]) -> float:
    """Weighted similarity between two signals for novelty purposes."""
    signal_type = signal.get("signal_type")
    type_score = 1.0 if signal_type and signal_type == other.get("signal_type") else 0.0
    brand_score = jaccard_similarity(
        set(signal.get("brands_mentioned") or []),
        set(other.get("brands_mentioned") or []),
    )
    feature_score = jaccard_similarity(
        set(signal.get("features_mentioned") or []),
        set(other.get("features_mentioned") or []),
    )
    source_score = jaccard_similarity(
        set(signal.get("source_article_ids") or []),
        set(other.get("source_article_ids") or []),
    )
    return (
        type_score * SIGNAL_TYPE_WEIGHT
        + brand_score * SIGNAL_BRAND_WEIGHT
        + feature_score * SIGNAL_FEATURE_WEIGHT
        + source_score * SIGNAL_SOURCE_WEIGHT
    )


def score_signal_novelty(signal: dict[str, Any], recent_signals: list[dict[str, Any]]) -> float:
    """Score how novel a signal is against recent signals (snake_case keys).

    Compares against every recent signal on signal type match, brand
    overlap, feature overlap, and source-article overlap. Novelty is 1.0
    minus the maximum similarity found, clamped to [0.0, 1.0]. The signal
    itself is skipped. No recent signals means fully novel (1.0).
    """
    max_similarity = 0.0
    for recent in recent_signals:
        if recent.get("id") == signal.get("id"):
            continue
        max_similarity = max(max_similarity, _signal_similarity(signal, recent))
    return min(1.0, max(0.0, 1.0 - max_similarity))


async def score_article_batch(
    articles: list[dict[str, Any]],
    lookback_days: int = DEFAULT_ARTICLE_LOOKBACK_DAYS,
) -> list[dict[str, Any]]:
    """Score novelty for a batch of articles against recent Firestore coverage.

    Fetches processed articles from the last ``lookback_days`` days and
    scores each input article against them, returning one
    ``{"article_id", "novelty_score"}`` dict per input. Nothing is written
    to Firestore. A fetch failure or a single scoring failure is logged
    and never raised; affected articles score the safe default 1.0.
    """
    if not articles:
        return []
    recent: list[dict[str, Any]] = []
    try:
        fetched = await get_recent_processed_articles(hours=lookback_days * 24)
        # The db layer returns snake_case keys (docs/tech-debt.md) but the
        # scoring functions compare camelCase Firestore doc fields.
        recent = [_keys_to_camel(item) for item in fetched]
    except Exception:
        logger.exception("failed to fetch recent articles, scoring against empty set")
    results: list[dict[str, Any]] = []
    for article in articles:
        article_id = str(article.get("id"))
        try:
            score = score_article_novelty(article, recent)
        except Exception:
            logger.exception(f"novelty scoring failed article_id={article_id}")
            score = 1.0
        results.append({"article_id": article_id, "novelty_score": score})
    logger.info(f"scored novelty for {len(results)} articles against {len(recent)} recent")
    return results


async def score_signal_batch(
    signals: list[dict[str, Any]],
    lookback_days: int = DEFAULT_SIGNAL_LOOKBACK_DAYS,
) -> list[dict[str, Any]]:
    """Score novelty for a batch of signals against recent Firestore signals.

    Fetches signals from the last ``lookback_days`` days and scores each
    input signal against them, returning one
    ``{"signal_id", "novelty_score"}`` dict per input. Nothing is written
    to Firestore. A fetch failure or a single scoring failure is logged
    and never raised; affected signals score the safe default 1.0.
    """
    if not signals:
        return []
    recent: list[dict[str, Any]] = []
    try:
        recent = await get_recent_signals(days=lookback_days)
    except Exception:
        logger.exception("failed to fetch recent signals, scoring against empty set")
    results: list[dict[str, Any]] = []
    for signal in signals:
        signal_id = str(signal.get("id"))
        try:
            score = score_signal_novelty(signal, recent)
        except Exception:
            logger.exception(f"novelty scoring failed signal_id={signal_id}")
            score = 1.0
        results.append({"signal_id": signal_id, "novelty_score": score})
    logger.info(f"scored novelty for {len(results)} signals against {len(recent)} recent")
    return results
