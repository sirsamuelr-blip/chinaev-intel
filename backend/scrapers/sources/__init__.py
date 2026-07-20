"""Per-source scrapers: one file per source, extending StaticScraper or DynamicScraper."""

from __future__ import annotations

from .autohome import AutohomeScraper
from .baidu_news import BaiduNewsScraper
from .cnevpost import CnEVPostScraper
from .gasgoo import GasgooScraper

__all__ = ["AutohomeScraper", "BaiduNewsScraper", "CnEVPostScraper", "GasgooScraper"]
