"""Read-only competitive comparison analysis over brands, vehicles, and features.

Answers three questions for the subscriber dashboard (Phase 5) and the
weekly digest (Phase 4): who has what (feature matrix by brand), who
shipped first (feature timeline), and what you get for your money
(price-to-feature analysis). Pure Firestore queries plus data
transformation — nothing is written to Firestore and no LLM calls are
made. This module is standalone — it is not wired into the pipeline
runner yet.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from db.firestore import (
    _to_aware_datetime,
    get_all_features,
    get_all_vehicles,
    get_brand_by_name_en,
    get_features_by_brand,
    get_vehicles_by_brand,
)

logger = logging.getLogger(__name__)

PRICE_TIER_BUDGET_MAX = 150_000
PRICE_TIER_MID_MAX = 300_000
PRICE_TIER_PREMIUM_MAX = 500_000
PRICE_TIERS = ("budget", "mid", "premium", "luxury", "unknown")

_PRICE_NUMBER_RE = re.compile(r"\d+")
_UNDATED_SORT_KEY = datetime.max.replace(tzinfo=UTC)

# All dicts flowing through this module use snake_case keys (db layer
# convention) and hold heterogeneous Firestore values (str, int, bool,
# list, map, ...), so dict values are typed as Any throughout.


def _serialize_date(value: Any) -> str | None:
    """Coerce a Firestore date value to an ISO 8601 string, or None."""
    parsed = _to_aware_datetime(value)
    if parsed is None:
        return None
    return parsed.isoformat()


def _feature_summary(feature: dict[str, Any]) -> dict[str, Any]:
    """Project a feature doc to the fields the comparison payloads expose."""
    return {
        "feature_name_en": feature.get("feature_name_en", ""),
        "description": feature.get("description", ""),
        "first_seen_date": _serialize_date(feature.get("first_seen_date")),
        "supplier": feature.get("supplier"),
    }


def _group_features_by_category(features: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group feature summaries by category; uncategorized docs get a fallback key."""
    by_category: dict[str, list[dict[str, Any]]] = {}
    for feature in features:
        category = str(feature.get("category") or "uncategorized")
        by_category.setdefault(category, []).append(_feature_summary(feature))
    return by_category


async def get_feature_matrix(category: str | None = None) -> dict[str, Any]:
    """Build a brands-vs-features matrix, optionally scoped to one category.

    Groups every feature doc by brand, then by category. Features without
    a ``brand_name_en`` are skipped — a brandless feature cannot be
    placed in the matrix. ``brands`` and ``categories`` are sorted and
    only cover populated entries; an empty features collection yields an
    empty matrix with zero counts.
    """
    features = await get_all_features(category)
    features_by_brand: dict[str, list[dict[str, Any]]] = {}
    total_features = 0
    for feature in features:
        brand = feature.get("brand_name_en")
        if not brand:
            logger.debug(f"skipping feature without brand_name_en id={feature.get('id')}")
            continue
        features_by_brand.setdefault(str(brand), []).append(feature)
        total_features += 1
    matrix = {
        brand: _group_features_by_category(brand_features)
        for brand, brand_features in features_by_brand.items()
    }
    brands = sorted(matrix)
    categories = sorted({cat for by_category in matrix.values() for cat in by_category})
    return {
        "matrix": matrix,
        "brands": brands,
        "categories": categories,
        "total_features": total_features,
        "total_brands": len(brands),
    }


async def get_feature_timeline(
    category: str | None = None, brand: str | None = None
) -> dict[str, Any]:
    """Return features ordered by ``first_seen_date``, oldest first.

    Optional category and/or brand filters (category is pushed down to
    the Firestore query; brand is filtered in Python). Features without
    a parseable ``first_seen_date`` sort after all dated features, so
    the timeline reads as who shipped first and who followed.
    """
    features = await get_all_features(category)
    if brand is not None:
        features = [feature for feature in features if feature.get("brand_name_en") == brand]
    timeline: list[dict[str, Any]] = []
    for feature in features:
        entry = _feature_summary(feature)
        entry["brand_name_en"] = feature.get("brand_name_en", "")
        entry["category"] = feature.get("category", "")
        entry["launch_type"] = feature.get("launch_type")
        timeline.append(entry)
    timeline.sort(
        key=lambda entry: _to_aware_datetime(entry["first_seen_date"]) or _UNDATED_SORT_KEY
    )
    return {
        "timeline": timeline,
        "filters_applied": {"category": category, "brand": brand},
        "total_features": len(timeline),
    }


def _build_brand_entry(
    brand_doc: dict[str, Any],
    features: list[dict[str, Any]],
    vehicles: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble one brand's side of the comparison payload."""
    features_by_category = _group_features_by_category(features)
    return {
        "brand_info": {
            "name_en": brand_doc.get("name_en", ""),
            "name_zh": brand_doc.get("name_zh", ""),
            "parent_group": brand_doc.get("parent_group", ""),
            "ev_focus": brand_doc.get("ev_focus", False),
        },
        "vehicle_count": len(vehicles),
        "vehicles": [
            {
                "model_name_en": vehicle.get("model_name_en", ""),
                "segment": vehicle.get("segment"),
                "powertrain": vehicle.get("powertrain"),
                "price_range_cny": vehicle.get("price_range_cny"),
            }
            for vehicle in vehicles
        ],
        "feature_count": len(features),
        "features_by_category": features_by_category,
        "categories_covered": sorted(features_by_category),
    }


async def get_brand_comparison(brand_names: list[str]) -> dict[str, Any]:
    """Compare two or more brands side by side.

    Fetches each brand's doc, features, and vehicles; unknown brand
    names land in ``brands_not_found`` instead of raising. The
    ``category_comparison`` map holds a feature count for every found
    brand in every populated category (0 when a brand has none), so the
    matrix stays rectangular for the dashboard.
    """
    brands: dict[str, dict[str, Any]] = {}
    brands_not_found: list[str] = []
    for name in brand_names:
        brand_doc = await get_brand_by_name_en(name)
        if brand_doc is None:
            logger.info(f"brand not found for comparison name={name}")
            brands_not_found.append(name)
            continue
        features = await get_features_by_brand(name)
        vehicles = await get_vehicles_by_brand(name)
        brands[name] = _build_brand_entry(brand_doc, features, vehicles)
    all_categories = sorted(
        {cat for entry in brands.values() for cat in entry["categories_covered"]}
    )
    category_comparison = {
        category: {
            name: len(entry["features_by_category"].get(category, []))
            for name, entry in brands.items()
        }
        for category in all_categories
    }
    return {
        "brands": brands,
        "category_comparison": category_comparison,
        "brands_not_found": brands_not_found,
    }


def _parse_price_lower_bound(price_range: Any) -> int | None:
    """Extract the lower-bound CNY amount from a price range doc value.

    Handles "150,000-200,000" and "150000-200000". Returns None for
    missing, non-string, or digit-free values — callers treat those as
    unknown pricing rather than raising.
    """
    if not isinstance(price_range, str):
        return None
    match = _PRICE_NUMBER_RE.search(price_range.replace(",", ""))
    if match is None:
        return None
    return int(match.group())


def _classify_price_tier(price_range: Any) -> str:
    """Classify a vehicle into a price tier by the lower bound of its range."""
    lower = _parse_price_lower_bound(price_range)
    if lower is None:
        return "unknown"
    if lower < PRICE_TIER_BUDGET_MAX:
        return "budget"
    if lower < PRICE_TIER_MID_MAX:
        return "mid"
    if lower <= PRICE_TIER_PREMIUM_MAX:
        return "premium"
    return "luxury"


def _build_tier_stats(
    vehicles: list[dict[str, Any]],
    features_by_brand: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Compute one tier's vehicle listing and average feature counts.

    Each vehicle's feature set is its brand's features — features are
    promoted at brand level (see processing/entities.py), so brand
    coverage is the per-vehicle proxy.
    """
    vehicle_entries: list[dict[str, Any]] = []
    category_totals: dict[str, int] = {}
    total_feature_count = 0
    for vehicle in vehicles:
        brand = str(vehicle.get("brand_name_en") or "")
        vehicle_entries.append(
            {
                "brand": brand,
                "model": vehicle.get("model_name_en", ""),
                "price_range_cny": vehicle.get("price_range_cny"),
            }
        )
        for feature in features_by_brand.get(brand, []):
            category = str(feature.get("category") or "uncategorized")
            category_totals[category] = category_totals.get(category, 0) + 1
            total_feature_count += 1
    count = len(vehicles)
    avg_by_category = (
        {category: total / count for category, total in sorted(category_totals.items())}
        if count
        else {}
    )
    return {
        "vehicle_count": count,
        "vehicles": vehicle_entries,
        "avg_features_by_category": avg_by_category,
        "total_avg_features": total_feature_count / count if count else 0.0,
    }


async def get_price_feature_analysis(segment: str | None = None) -> dict[str, Any]:
    """Compare feature coverage per price tier, optionally scoped to a segment.

    Groups vehicles into CNY price tiers by the lower bound of
    ``price_range_cny`` (unparseable or missing prices land in
    "unknown") and averages each tier's per-category feature counts
    using each vehicle's brand-level feature set. Every tier key is
    always present so the payload shape is stable for the dashboard.
    """
    vehicles = await get_all_vehicles(segment)
    features = await get_all_features()
    features_by_brand: dict[str, list[dict[str, Any]]] = {}
    for feature in features:
        brand = feature.get("brand_name_en")
        if brand:
            features_by_brand.setdefault(str(brand), []).append(feature)
    vehicles_by_tier: dict[str, list[dict[str, Any]]] = {tier: [] for tier in PRICE_TIERS}
    for vehicle in vehicles:
        vehicles_by_tier[_classify_price_tier(vehicle.get("price_range_cny"))].append(vehicle)
    tiers = {
        tier: _build_tier_stats(tier_vehicles, features_by_brand)
        for tier, tier_vehicles in vehicles_by_tier.items()
    }
    return {
        "tiers": tiers,
        "filters_applied": {"segment": segment},
    }
