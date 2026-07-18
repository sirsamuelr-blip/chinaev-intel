"""Per-source scrapers: one file per source, extending StaticScraper or DynamicScraper."""

from __future__ import annotations

from .gasgoo import GasgooScraper

__all__ = ["GasgooScraper"]
