"""Cross-source article deduplication using multi-signal similarity.

Detects when multiple sources report the same story (ADR 004). Phase 1
deduplicates by URL within a source; this module compares processed
articles across sources with a weighted five-dimension similarity score
over fields the Phase 1 LLM pipeline already extracted — no LLM calls.
``deduplicate_articles`` is analysis only; ``mark_duplicates`` is the
separate Firestore write step so results can be reviewed (Phase 3 admin
queue) before being committed. This module is standalone — it is not
wired into the pipeline runner yet.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from db.firestore import update_article_dedup_fields

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.75
DEDUP_WINDOW_HOURS = 72

# Weights per ADR 004
TITLE_WEIGHT = 0.3
BRAND_WEIGHT = 0.25
VEHICLE_WEIGHT = 0.2
FEATURE_CATEGORY_WEIGHT = 0.15
DATE_PROXIMITY_WEIGHT = 0.1

# Article dicts hold heterogeneous Firestore doc values (str, int, list,
# map, ...), so dict values are typed as Any throughout this module. All
# article dicts use camelCase keys (Firestore doc shape).


def title_similarity(title_a: str | None, title_b: str | None) -> float:
    """Fuzzy string match on English titles using SequenceMatcher.

    Returns 0.0-1.0. Comparison is case-insensitive. A None or empty
    title scores 0.0 — a missing title is never a match signal.
    """
    if not title_a or not title_b:
        return 0.0
    return SequenceMatcher(None, title_a.lower(), title_b.lower()).ratio()


def jaccard_similarity(set_a: set[str], set_b: set[str]) -> float:
    """Jaccard index on two string sets: |intersection| / |union|.

    Comparison is case-insensitive. Two empty sets score 0.0 — shared
    emptiness is not a match signal.
    """
    lowered_a = {item.lower() for item in set_a}
    lowered_b = {item.lower() for item in set_b}
    union = lowered_a | lowered_b
    if not union:
        return 0.0
    return len(lowered_a & lowered_b) / len(union)


def _parse_date(value: datetime | str | None) -> datetime | None:
    """Coerce an ISO 8601 string or datetime to a timezone-aware datetime.

    Naive datetimes are assumed UTC (scrapers normalize to UTC before
    writing). Returns None for missing or unparseable values — callers
    treat those as "no date" rather than raising.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            logger.warning(f"unparseable date value: {value!r}")
            return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def date_within_window(
    date_a: datetime | str | None,
    date_b: datetime | str | None,
    hours: int = DEDUP_WINDOW_HOURS,
) -> bool:
    """True if the two dates are within ``hours`` of each other (inclusive).

    Accepts timezone-aware or naive datetimes (naive assumed UTC) and ISO
    8601 strings. Missing or unparseable dates count as outside the window.
    """
    parsed_a = _parse_date(date_a)
    parsed_b = _parse_date(date_b)
    if parsed_a is None or parsed_b is None:
        return False
    return abs(parsed_a - parsed_b) <= timedelta(hours=hours)


def extract_feature_categories(features_extracted: list[dict[str, Any]] | None) -> set[str]:
    """Extract the unique ``category`` values from a featuresExtracted array.

    Handles None, empty lists, and items without a category.
    """
    if not features_extracted:
        return set()
    return {
        feature["category"]
        for feature in features_extracted
        if isinstance(feature, dict) and feature.get("category")
    }


def compute_similarity(article_a: dict[str, Any], article_b: dict[str, Any]) -> float:
    """Compute the weighted composite similarity score between two articles.

    Articles outside the publish-date window score 0.0 immediately with no
    other dimension checked; inside it, the date-proximity dimension
    contributes its full weight (the binary dimension per ADR 004). A
    missing field scores 0.0 on its dimension rather than raising.
    """
    if not date_within_window(article_a.get("publishDate"), article_b.get("publishDate")):
        return 0.0
    title_score = title_similarity(article_a.get("titleEn"), article_b.get("titleEn"))
    brand_score = jaccard_similarity(
        set(article_a.get("brandsMentioned") or []),
        set(article_b.get("brandsMentioned") or []),
    )
    vehicle_score = jaccard_similarity(
        set(article_a.get("vehiclesMentioned") or []),
        set(article_b.get("vehiclesMentioned") or []),
    )
    category_score = jaccard_similarity(
        extract_feature_categories(article_a.get("featuresExtracted")),
        extract_feature_categories(article_b.get("featuresExtracted")),
    )
    return (
        title_score * TITLE_WEIGHT
        + brand_score * BRAND_WEIGHT
        + vehicle_score * VEHICLE_WEIGHT
        + category_score * FEATURE_CATEGORY_WEIGHT
        + DATE_PROXIMITY_WEIGHT
    )


async def find_duplicates_for_article(
    article: dict[str, Any],
    candidate_articles: list[dict[str, Any]],
) -> list[tuple[str, float]]:
    """Compare one article against candidates and return matches above threshold.

    Candidates from the same source (or the article itself) are skipped —
    same-source dedup is handled by URL in the runner. A single comparison
    failure is logged and skipped, never raised. Returns (candidate doc ID,
    score) tuples sorted by score descending.
    """
    matches: list[tuple[str, float]] = []
    for candidate in candidate_articles:
        if candidate.get("id") == article.get("id"):
            continue
        if candidate.get("sourceName") == article.get("sourceName"):
            continue
        try:
            score = compute_similarity(article, candidate)
        except Exception:
            logger.exception(
                f"similarity comparison failed article={article.get('id')} "
                f"candidate={candidate.get('id')}"
            )
            continue
        if score >= SIMILARITY_THRESHOLD:
            matches.append((str(candidate["id"]), score))
    matches.sort(key=lambda match: match[1], reverse=True)
    return matches


def _above_threshold_pairs(articles: list[dict[str, Any]]) -> dict[tuple[str, str], float]:
    """Score every cross-source article pair, keeping those at or above threshold.

    Keys are (doc ID, doc ID) tuples in sorted order. A single comparison
    failure is logged and skipped, never raised.
    """
    pairs: dict[tuple[str, str], float] = {}
    for index, article_a in enumerate(articles):
        for article_b in articles[index + 1 :]:
            if article_a.get("sourceName") == article_b.get("sourceName"):
                continue
            try:
                score = compute_similarity(article_a, article_b)
            except Exception:
                logger.exception(
                    f"similarity comparison failed article={article_a.get('id')} "
                    f"candidate={article_b.get('id')}"
                )
                continue
            if score >= SIMILARITY_THRESHOLD:
                id_a, id_b = str(article_a["id"]), str(article_b["id"])
                pairs[(id_a, id_b) if id_a < id_b else (id_b, id_a)] = score
    return pairs


def _connected_components(ids: list[str], pairs: Iterable[tuple[str, str]]) -> list[list[str]]:
    """Cluster doc IDs into connected components over the matched pairs.

    Union-find with path compression: if A matches B and B matches C, all
    three land in one component even when A and C never matched directly.
    """
    parent = {article_id: article_id for article_id in ids}

    def find(article_id: str) -> str:
        root = article_id
        while parent[root] != root:
            root = parent[root]
        while parent[article_id] != root:
            parent[article_id], article_id = root, parent[article_id]
        return root

    for id_a, id_b in pairs:
        parent[find(id_a)] = find(id_b)

    components: dict[str, list[str]] = {}
    for article_id in ids:
        components.setdefault(find(article_id), []).append(article_id)
    return list(components.values())


def _select_canonical(members: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the canonical article for a duplicate group.

    An article already marked canonical in a prior run (has a
    duplicateGroupId and isDuplicate == False) keeps its role, so new
    articles join the existing group. Otherwise the earliest scrapeDate
    wins, with higher relevanceScore breaking ties.
    """
    for member in members:
        if member.get("duplicateGroupId") and member.get("isDuplicate") is False:
            return member

    def sort_key(member: dict[str, Any]) -> tuple[datetime, float]:
        scrape_date = _parse_date(member.get("scrapeDate")) or datetime.max.replace(tzinfo=UTC)
        relevance = member.get("relevanceScore") or 0
        return (scrape_date, -relevance)

    return min(members, key=sort_key)


def _group_id_for(members: list[dict[str, Any]]) -> str:
    """Reuse an existing duplicateGroupId if any member has one, else mint one."""
    for member in members:
        existing = member.get("duplicateGroupId")
        if existing:
            return str(existing)
    return str(uuid.uuid4())


async def deduplicate_articles(articles: list[dict[str, Any]]) -> dict[str, Any]:
    """Find cross-source duplicate groups in a batch of processed articles.

    Every article must carry an ``id`` key. Compares all pairs from
    different sources within the publish-date window and clusters
    above-threshold pairs into groups. Analysis only — nothing is written
    to Firestore; pass the returned ``groups`` to ``mark_duplicates`` to
    persist. Each group's ``similarity_scores`` lists the above-threshold
    pair scores inside that group, descending.
    """
    pair_scores = _above_threshold_pairs(articles)
    ids = [str(article["id"]) for article in articles]
    by_id = {str(article["id"]): article for article in articles}

    groups: list[dict[str, Any]] = []
    for component in _connected_components(ids, pair_scores):
        if len(component) < 2:
            continue
        members = [by_id[member_id] for member_id in component]
        canonical_id = str(_select_canonical(members)["id"])
        member_set = set(component)
        scores = sorted(
            (
                score
                for pair, score in pair_scores.items()
                if pair[0] in member_set and pair[1] in member_set
            ),
            reverse=True,
        )
        groups.append(
            {
                "duplicate_group_id": _group_id_for(members),
                "canonical_id": canonical_id,
                "duplicate_ids": [m for m in component if m != canonical_id],
                "similarity_scores": scores,
            }
        )

    marked = sum(len(group["duplicate_ids"]) for group in groups)
    logger.info(
        f"dedup analyzed {len(articles)} articles: {len(groups)} groups, {marked} duplicates"
    )
    return {
        "total_compared": len(articles),
        "duplicate_groups_found": len(groups),
        "articles_marked_duplicate": marked,
        "groups": groups,
    }


async def mark_duplicates(groups: list[dict[str, Any]]) -> int:
    """Persist dedup results from ``deduplicate_articles`` to Firestore.

    The canonical article gets isDuplicate=False / canonicalArticleId=None;
    every duplicate points at the canonical. All group members share the
    duplicateGroupId. A single write failure is logged and skipped.
    Returns the number of article docs updated (canonicals included).
    """
    updated = 0
    for group in groups:
        group_id = str(group["duplicate_group_id"])
        canonical_id = str(group["canonical_id"])
        writes: list[tuple[str, bool, str | None]] = [(canonical_id, False, None)]
        writes.extend((str(dup_id), True, canonical_id) for dup_id in group["duplicate_ids"])
        for article_id, is_duplicate, canonical_ref in writes:
            try:
                await update_article_dedup_fields(article_id, is_duplicate, canonical_ref, group_id)
            except Exception:
                logger.exception(f"failed to mark dedup fields doc_id={article_id}")
                continue
            updated += 1
    return updated
