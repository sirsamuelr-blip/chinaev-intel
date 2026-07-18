"""Scraper package: shared base classes and per-source scrapers."""

from __future__ import annotations

from .base import BaseScraper
from .dynamic import DynamicScraper
from .sources.baidu_news import BaiduNewsScraper
from .sources.cnevpost import CnEVPostScraper
from .sources.gasgoo import GasgooScraper
from .static import StaticScraper

__all__ = [
    "BaiduNewsScraper",
    "BaseScraper",
    "CnEVPostScraper",
    "DynamicScraper",
    "GasgooScraper",
    "StaticScraper",
]
