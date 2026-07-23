"""Async Firestore helpers for the pipeline collections.

Covers ``articles`` and ``scraper_health`` (Phase 1 scraper pipeline) plus
``brands``, ``vehicles``, and ``features`` (Phase 2 entity promotion). The
Firestore client is initialized lazily on first use so tests can swap in a
mock without touching real Firebase credentials. Python code uses
snake_case keys; Firestore documents use camelCase (see
docs/firestore-schema.md) — the ``keys_to_camel`` / ``_keys_to_snake``
helpers convert at the read/write boundary. ``keys_to_camel`` is public:
the runner uses it to bridge db-layer reads into the camelCase doc shape
the Phase 2 modules expect.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from google.cloud.firestore import SERVER_TIMESTAMP
from google.cloud.firestore_v1.base_query import FieldFilter

from config import settings

if TYPE_CHECKING:
    from google.cloud.firestore import AsyncClient

logger = logging.getLogger(__name__)

ARTICLES_COLLECTION = "articles"
SCRAPER_HEALTH_COLLECTION = "scraper_health"
BRANDS_COLLECTION = "brands"
VEHICLES_COLLECTION = "vehicles"
FEATURES_COLLECTION = "features"
SIGNALS_COLLECTION = "signals"

_db: AsyncClient | None = None


def get_db() -> AsyncClient:
    """Return the shared Firestore async client, initializing it on first use.

    Firebase initialization is deferred until the first call so importing
    this module never requires credentials. Tests replace ``_db`` with a
    mock client before any helper runs.
    """
    global _db
    if _db is None:
        import firebase_admin
        from firebase_admin import credentials, firestore

        if not firebase_admin._apps:
            cred = credentials.Certificate(settings.GOOGLE_APPLICATION_CREDENTIALS)
            firebase_admin.initialize_app(cred, {"projectId": settings.FIREBASE_PROJECT_ID})

        _db = firestore.AsyncClient()
    return _db


def _snake_to_camel(name: str) -> str:
    """Convert a snake_case field name to camelCase (``source_url`` -> ``sourceUrl``)."""
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def _camel_to_snake(name: str) -> str:
    """Convert a camelCase field name to snake_case (``sourceUrl`` -> ``source_url``)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


# Firestore document values are heterogeneous (str, int, bool, list, map, ...),
# so dict values are typed as Any throughout this module.
def keys_to_camel(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``data`` with top-level keys converted to camelCase."""
    return {_snake_to_camel(key): value for key, value in data.items()}


def _keys_to_snake(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``data`` with top-level keys converted to snake_case."""
    return {_camel_to_snake(key): value for key, value in data.items()}


async def save_article(article: dict[str, Any]) -> str:
    """Write a new article document and return its auto-generated doc ID.

    Keys are converted to camelCase. ``processed`` is forced to False and
    ``processingError`` to None: every article starts in the unprocessed
    queue for the LLM pipeline.
    """
    data = keys_to_camel(article)
    data["processed"] = False
    data["processingError"] = None
    _, doc_ref = await get_db().collection(ARTICLES_COLLECTION).add(data)
    doc_id = str(doc_ref.id)
    logger.info(
        f"saved article source={article.get('source_name')} "
        f"url={article.get('source_url')} doc_id={doc_id}"
    )
    return doc_id


async def article_exists(source_url: str) -> bool:
    """Return True if an article with this ``sourceUrl`` is already stored.

    Deduplication check: the runner calls this before scraping a URL.
    """
    query = (
        get_db()
        .collection(ARTICLES_COLLECTION)
        .where(filter=FieldFilter("sourceUrl", "==", source_url))
        .limit(1)
    )
    snapshots = await query.get()
    return len(snapshots) > 0


async def get_unprocessed_articles(limit: int = 50) -> list[dict[str, Any]]:
    """Return up to ``limit`` unprocessed articles, oldest scrape first.

    Each dict has snake_case keys plus an ``id`` key holding the document
    ID. This is the LLM pipeline's read path.
    """
    query = (
        get_db()
        .collection(ARTICLES_COLLECTION)
        .where(filter=FieldFilter("processed", "==", False))
        .order_by("scrapeDate")
        .limit(limit)
    )
    snapshots = await query.get()
    articles: list[dict[str, Any]] = []
    for snapshot in snapshots:
        article = _keys_to_snake(snapshot.to_dict() or {})
        article["id"] = snapshot.id
        articles.append(article)
    return articles


async def update_article_after_processing(doc_id: str, extracted_data: dict[str, Any]) -> None:
    """Write LLM extraction results back to an article and mark it processed.

    ``extracted_data`` holds snake_case pipeline fields (title_en, body_en,
    relevance_score, content_type, brands_mentioned, vehicles_mentioned,
    features_extracted, competitive_signal); keys are converted to
    camelCase. ``processingError`` is cleared.
    """
    updates = keys_to_camel(extracted_data)
    updates["processed"] = True
    updates["processingError"] = None
    await get_db().collection(ARTICLES_COLLECTION).document(doc_id).update(updates)
    logger.info(
        f"processed article doc_id={doc_id} relevance_score={extracted_data.get('relevance_score')}"
    )


async def set_article_processing_error(doc_id: str, error_message: str) -> None:
    """Record a processing failure on an article without marking it processed.

    The article stays in the unprocessed queue so the next pipeline run
    retries it.
    """
    await (
        get_db()
        .collection(ARTICLES_COLLECTION)
        .document(doc_id)
        .update({"processingError": error_message})
    )
    logger.warning(f"processing error doc_id={doc_id} error={error_message}")


def _to_aware_datetime(value: Any) -> datetime | None:
    """Coerce an ISO 8601 string or datetime doc value to a timezone-aware datetime.

    Naive datetimes are assumed UTC. Returns None for anything else —
    callers treat unparseable dates as missing rather than raising.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def get_recent_processed_articles(hours: int = 72) -> list[dict[str, Any]]:
    """Return processed articles scraped within the last ``hours`` hours.

    Newest scrape first. Each dict has snake_case keys plus an ``id`` key
    holding the document ID. This is the dedup module's read path.

    The date window is filtered in Python rather than in the query:
    combining ``processed == true`` with a ``scrapeDate`` range filter
    requires a composite index, and the processed set within a few days is
    small enough that over-fetching is cheaper than maintaining one.
    """
    query = (
        get_db()
        .collection(ARTICLES_COLLECTION)
        .where(filter=FieldFilter("processed", "==", True))
        .order_by("scrapeDate", direction="DESCENDING")
    )
    snapshots = await query.get()
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    articles: list[dict[str, Any]] = []
    for snapshot in snapshots:
        article = _keys_to_snake(snapshot.to_dict() or {})
        scrape_date = _to_aware_datetime(article.get("scrape_date"))
        if scrape_date is None or scrape_date < cutoff:
            continue
        article["id"] = snapshot.id
        articles.append(article)
    return articles


async def get_phase2_unprocessed_articles() -> list[dict[str, Any]]:
    """Return processed articles that have not been through Phase 2 yet.

    Newest scrape first. Each dict has snake_case keys plus an ``id`` key
    holding the document ID. This is the Phase 2 pipeline's work queue: an
    article qualifies once the LLM pipeline has set ``processed == True`` but
    the Phase 2 pass has not yet set ``phase2Processed``.

    The ``phase2Processed`` exclusion is applied in Python rather than as a
    Firestore ``!=`` filter: a ``!=`` query drops documents where the field
    is absent, but freshly extracted articles have no ``phase2Processed``
    field at all and are exactly the ones we want. A missing or falsey flag
    both count as not-yet-processed. This mirrors ``get_recent_processed_articles``
    (same ``processed == true`` + ``scrapeDate`` index, no extra composite).
    """
    query = (
        get_db()
        .collection(ARTICLES_COLLECTION)
        .where(filter=FieldFilter("processed", "==", True))
        .order_by("scrapeDate", direction="DESCENDING")
    )
    snapshots = await query.get()
    articles: list[dict[str, Any]] = []
    for snapshot in snapshots:
        article = _keys_to_snake(snapshot.to_dict() or {})
        if article.get("phase2_processed") is True:
            continue
        article["id"] = snapshot.id
        articles.append(article)
    return articles


async def mark_article_phase2_processed(article_id: str) -> None:
    """Set ``phase2Processed = True`` on an article after a successful Phase 2 pass.

    Called by the runner only once every Phase 2 step has completed for the
    run, so the article is excluded from ``get_phase2_unprocessed_articles``
    on the next cron cycle. A failed run leaves the flag unset and the
    article is retried.
    """
    await (
        get_db()
        .collection(ARTICLES_COLLECTION)
        .document(article_id)
        .update({"phase2Processed": True})
    )
    logger.info(f"marked phase2Processed doc_id={article_id}")


async def update_article_dedup_fields(
    article_id: str,
    is_duplicate: bool,
    canonical_article_id: str | None,
    duplicate_group_id: str | None,
) -> None:
    """Update the deduplication fields on an article doc (see ADR 004).

    Canonical articles get ``isDuplicate=False`` and a null
    ``canonicalArticleId``; duplicates point at their canonical. All
    members of a group share the ``duplicateGroupId``.
    """
    await (
        get_db()
        .collection(ARTICLES_COLLECTION)
        .document(article_id)
        .update(
            {
                "isDuplicate": is_duplicate,
                "canonicalArticleId": canonical_article_id,
                "duplicateGroupId": duplicate_group_id,
            }
        )
    )
    logger.info(
        f"updated dedup fields doc_id={article_id} is_duplicate={is_duplicate} "
        f"group={duplicate_group_id}"
    )


async def get_articles_by_duplicate_group(duplicate_group_id: str) -> list[dict[str, Any]]:
    """Return all articles sharing this ``duplicateGroupId``.

    Each dict has snake_case keys plus an ``id`` key. Used to check
    whether a new article matches an existing duplicate group.
    """
    query = (
        get_db()
        .collection(ARTICLES_COLLECTION)
        .where(filter=FieldFilter("duplicateGroupId", "==", duplicate_group_id))
    )
    snapshots = await query.get()
    articles: list[dict[str, Any]] = []
    for snapshot in snapshots:
        article = _keys_to_snake(snapshot.to_dict() or {})
        article["id"] = snapshot.id
        articles.append(article)
    return articles


async def _find_one(collection: str, filters: dict[str, Any]) -> dict[str, Any] | None:
    """Return the first doc matching all equality ``filters``, or None.

    ``filters`` maps camelCase field names to required values. The returned
    dict has snake_case keys plus an ``id`` key holding the document ID,
    matching the shape of ``get_unprocessed_articles`` results.
    """
    query: Any = get_db().collection(collection)  # google types are Any (ignore_missing_imports)
    for field_name, value in filters.items():
        query = query.where(filter=FieldFilter(field_name, "==", value))
    snapshots = await query.limit(1).get()
    if not snapshots:
        return None
    doc = _keys_to_snake(snapshots[0].to_dict() or {})
    doc["id"] = snapshots[0].id
    return doc


async def _upsert(
    collection: str,
    match: dict[str, Any],
    data: dict[str, Any],
    create_only: dict[str, Any] | None = None,
) -> str:
    """Create or update a doc in ``collection`` matched by equality ``match``.

    ``data`` keys are snake_case and converted to camelCase on write;
    ``lastUpdated`` is set to the server timestamp on both paths.
    ``create_only`` fields (already camelCase) are written only when a new
    doc is created — existing docs never have them overwritten. Returns the
    doc ID.
    """
    payload = keys_to_camel(data)
    payload["lastUpdated"] = SERVER_TIMESTAMP
    existing = await _find_one(collection, match)
    if existing is not None:
        doc_id = str(existing["id"])
        await get_db().collection(collection).document(doc_id).update(payload)
        logger.info(f"updated {collection} doc doc_id={doc_id}")
        return doc_id
    if create_only:
        payload.update(create_only)
    _, doc_ref = await get_db().collection(collection).add(payload)
    doc_id = str(doc_ref.id)
    logger.info(f"created {collection} doc doc_id={doc_id}")
    return doc_id


async def get_brand_by_name_en(name_en: str) -> dict[str, Any] | None:
    """Return the brand doc with this canonical ``nameEn``, or None."""
    return await _find_one(BRANDS_COLLECTION, {"nameEn": name_en})


async def upsert_brand(brand_data: dict[str, Any]) -> str:
    """Create or update a brand doc keyed by ``name_en`` and return its doc ID.

    ``brand_data`` holds snake_case fields (name_en, name_zh, parent_group,
    ev_focus); ``lastUpdated`` is set to the server timestamp.
    """
    return await _upsert(BRANDS_COLLECTION, {"nameEn": brand_data["name_en"]}, brand_data)


async def get_vehicle_by_model(brand_id: str, model_name_en: str) -> dict[str, Any] | None:
    """Return the vehicle doc matching ``brandId`` + ``modelNameEn``, or None."""
    return await _find_one(VEHICLES_COLLECTION, {"brandId": brand_id, "modelNameEn": model_name_en})


async def upsert_vehicle(vehicle_data: dict[str, Any]) -> str:
    """Create or update a vehicle doc keyed by brand + model and return its doc ID.

    ``vehicle_data`` holds snake_case fields (brand_id, brand_name_en,
    model_name_en, plus optional schema fields).
    """
    return await _upsert(
        VEHICLES_COLLECTION,
        {"brandId": vehicle_data["brand_id"], "modelNameEn": vehicle_data["model_name_en"]},
        vehicle_data,
    )


async def get_feature(brand_id: str, feature_name: str, category: str) -> dict[str, Any] | None:
    """Return the feature doc matching ``brandId`` + ``featureNameEn`` + ``category``."""
    return await _find_one(
        FEATURES_COLLECTION,
        {"brandId": brand_id, "featureNameEn": feature_name, "category": category},
    )


async def upsert_feature(feature_data: dict[str, Any]) -> str:
    """Create or update a feature doc and return its doc ID.

    Matched on brand + feature name + category. New docs get
    ``firstSeenDate`` set to the server timestamp; updates never touch it.
    ``lastUpdated`` (beyond the original schema, intentionally) is set on
    both paths.
    """
    return await _upsert(
        FEATURES_COLLECTION,
        {
            "brandId": feature_data["brand_id"],
            "featureNameEn": feature_data["feature_name_en"],
            "category": feature_data["category"],
        },
        feature_data,
        create_only={"firstSeenDate": SERVER_TIMESTAMP},
    )


async def get_all_features(category: str | None = None) -> list[dict[str, Any]]:
    """Return all feature docs, optionally filtered by ``category``.

    Each dict has snake_case keys plus an ``id`` key holding the document
    ID. This is the signal detection module's read path for the
    trickle-down check.
    """
    query: Any = get_db().collection(FEATURES_COLLECTION)  # google types are Any
    if category is not None:
        query = query.where(filter=FieldFilter("category", "==", category))
    snapshots = await query.get()
    features: list[dict[str, Any]] = []
    for snapshot in snapshots:
        feature = _keys_to_snake(snapshot.to_dict() or {})
        feature["id"] = snapshot.id
        features.append(feature)
    return features


async def get_features_by_brand(brand_name_en: str) -> list[dict[str, Any]]:
    """Return all feature docs with this ``brandNameEn``.

    Each dict has snake_case keys plus an ``id`` key holding the document
    ID. This is the comparison module's per-brand feature read path.
    """
    query = (
        get_db()
        .collection(FEATURES_COLLECTION)
        .where(filter=FieldFilter("brandNameEn", "==", brand_name_en))
    )
    snapshots = await query.get()
    features: list[dict[str, Any]] = []
    for snapshot in snapshots:
        feature = _keys_to_snake(snapshot.to_dict() or {})
        feature["id"] = snapshot.id
        features.append(feature)
    return features


async def get_vehicles_by_brand(brand_name_en: str) -> list[dict[str, Any]]:
    """Return all vehicle docs with this ``brandNameEn``.

    Each dict has snake_case keys plus an ``id`` key holding the document
    ID. This is the comparison module's per-brand vehicle read path.
    """
    query = (
        get_db()
        .collection(VEHICLES_COLLECTION)
        .where(filter=FieldFilter("brandNameEn", "==", brand_name_en))
    )
    snapshots = await query.get()
    vehicles: list[dict[str, Any]] = []
    for snapshot in snapshots:
        vehicle = _keys_to_snake(snapshot.to_dict() or {})
        vehicle["id"] = snapshot.id
        vehicles.append(vehicle)
    return vehicles


async def get_all_vehicles(segment: str | None = None) -> list[dict[str, Any]]:
    """Return all vehicle docs, optionally filtered by ``segment``.

    Each dict has snake_case keys plus an ``id`` key holding the document
    ID. This is the price-to-feature analysis read path.
    """
    query: Any = get_db().collection(VEHICLES_COLLECTION)  # google types are Any
    if segment is not None:
        query = query.where(filter=FieldFilter("segment", "==", segment))
    snapshots = await query.get()
    vehicles: list[dict[str, Any]] = []
    for snapshot in snapshots:
        vehicle = _keys_to_snake(snapshot.to_dict() or {})
        vehicle["id"] = snapshot.id
        vehicles.append(vehicle)
    return vehicles


async def save_signal(signal_data: dict[str, Any]) -> str:
    """Write a new signal document and return its auto-generated doc ID.

    ``signal_data`` holds snake_case fields (signal_type, title, summary,
    brands_mentioned, features_mentioned, source_article_ids,
    implications_for_western_oems, competitive_impact_score); keys are
    converted to camelCase. ``createdDate`` is set to the server timestamp
    and ``status`` defaults to "pending" (see docs/firestore-schema.md).
    """
    data = keys_to_camel(signal_data)
    data["createdDate"] = SERVER_TIMESTAMP
    data.setdefault("status", "pending")
    _, doc_ref = await get_db().collection(SIGNALS_COLLECTION).add(data)
    doc_id = str(doc_ref.id)
    logger.info(
        f"saved signal type={signal_data.get('signal_type')} "
        f"score={signal_data.get('competitive_impact_score')} doc_id={doc_id}"
    )
    return doc_id


async def get_signals_by_article_id(article_id: str) -> list[dict[str, Any]]:
    """Return all signals whose ``sourceArticleIds`` array contains this article.

    Each dict has snake_case keys plus an ``id`` key holding the document
    ID. Used to check which signals an article has already produced.
    """
    query = (
        get_db()
        .collection(SIGNALS_COLLECTION)
        .where(filter=FieldFilter("sourceArticleIds", "array_contains", article_id))
    )
    snapshots = await query.get()
    signals: list[dict[str, Any]] = []
    for snapshot in snapshots:
        signal = _keys_to_snake(snapshot.to_dict() or {})
        signal["id"] = snapshot.id
        signals.append(signal)
    return signals


async def get_recent_signals(days: int = 14) -> list[dict[str, Any]]:
    """Return signals created within the last ``days`` days.

    Newest first. Each dict has snake_case keys plus an ``id`` key holding
    the document ID. This is the novelty scoring module's read path.

    The date window is filtered in Python rather than in the query,
    matching ``get_recent_processed_articles``: the recent signal set is
    small enough that over-fetching is cheaper than range-filter plumbing.
    """
    query = get_db().collection(SIGNALS_COLLECTION).order_by("createdDate", direction="DESCENDING")
    snapshots = await query.get()
    cutoff = datetime.now(UTC) - timedelta(days=days)
    signals: list[dict[str, Any]] = []
    for snapshot in snapshots:
        signal = _keys_to_snake(snapshot.to_dict() or {})
        created_date = _to_aware_datetime(signal.get("created_date"))
        if created_date is None or created_date < cutoff:
            continue
        signal["id"] = snapshot.id
        signals.append(signal)
    return signals


async def save_health_metrics(metrics: dict[str, Any]) -> str:
    """Write a scraper health document and return its auto-generated doc ID.

    Keys are converted to camelCase and ``runTimestamp`` is set to the
    Firestore server timestamp.
    """
    data = keys_to_camel(metrics)
    data["runTimestamp"] = SERVER_TIMESTAMP
    _, doc_ref = await get_db().collection(SCRAPER_HEALTH_COLLECTION).add(data)
    doc_id = str(doc_ref.id)
    logger.info(
        f"saved health metrics source={metrics.get('source_name')} "
        f"status={metrics.get('status')} articles_ingested={metrics.get('articles_ingested')} "
        f"doc_id={doc_id}"
    )
    return doc_id
