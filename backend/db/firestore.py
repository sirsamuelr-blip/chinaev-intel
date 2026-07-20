"""Async Firestore helpers for the ``articles`` and ``scraper_health`` collections.

These are the two collections the Phase 1 scraper pipeline writes to. The
Firestore client is initialized lazily on first use so tests can swap in a
mock without touching real Firebase credentials. Python code uses
snake_case keys; Firestore documents use camelCase (see
docs/firestore-schema.md) — the private ``_keys_to_camel`` /
``_keys_to_snake`` helpers convert at the read/write boundary.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from google.cloud.firestore import SERVER_TIMESTAMP
from google.cloud.firestore_v1.base_query import FieldFilter

from config import settings

if TYPE_CHECKING:
    from google.cloud.firestore import AsyncClient

logger = logging.getLogger(__name__)

ARTICLES_COLLECTION = "articles"
SCRAPER_HEALTH_COLLECTION = "scraper_health"

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
def _keys_to_camel(data: dict[str, Any]) -> dict[str, Any]:
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
    data = _keys_to_camel(article)
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
    updates = _keys_to_camel(extracted_data)
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


async def save_health_metrics(metrics: dict[str, Any]) -> str:
    """Write a scraper health document and return its auto-generated doc ID.

    Keys are converted to camelCase and ``runTimestamp`` is set to the
    Firestore server timestamp.
    """
    data = _keys_to_camel(metrics)
    data["runTimestamp"] = SERVER_TIMESTAMP
    _, doc_ref = await get_db().collection(SCRAPER_HEALTH_COLLECTION).add(data)
    doc_id = str(doc_ref.id)
    logger.info(
        f"saved health metrics source={metrics.get('source_name')} "
        f"status={metrics.get('status')} articles_ingested={metrics.get('articles_ingested')} "
        f"doc_id={doc_id}"
    )
    return doc_id
