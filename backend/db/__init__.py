"""Database package: async Firestore helpers for the scraper pipeline."""

from __future__ import annotations

from .firestore import (
    article_exists,
    get_db,
    get_unprocessed_articles,
    save_article,
    save_health_metrics,
    set_article_processing_error,
    update_article_after_processing,
)

__all__ = [
    "article_exists",
    "get_db",
    "get_unprocessed_articles",
    "save_article",
    "save_health_metrics",
    "set_article_processing_error",
    "update_article_after_processing",
]
