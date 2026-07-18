"""Scraper package: shared base classes and per-source scrapers."""

from __future__ import annotations

from .base import BaseScraper
from .static import StaticScraper

__all__ = ["BaseScraper", "StaticScraper"]
