"""Scraper package: shared base classes and per-source scrapers."""

from __future__ import annotations

from .base import BaseScraper
from .sources.gasgoo import GasgooScraper
from .static import StaticScraper

__all__ = ["BaseScraper", "GasgooScraper", "StaticScraper"]
