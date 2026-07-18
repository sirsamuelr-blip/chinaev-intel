"""Scraper package: shared base classes and per-source scrapers."""

from __future__ import annotations

from .base import BaseScraper
from .sources.cnevpost import CnEVPostScraper
from .sources.gasgoo import GasgooScraper
from .static import StaticScraper

__all__ = ["BaseScraper", "CnEVPostScraper", "GasgooScraper", "StaticScraper"]
