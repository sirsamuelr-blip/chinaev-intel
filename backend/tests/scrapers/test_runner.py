"""Tests for the scraper runner orchestration.

Everything external is mocked: the scraper classes are replaced with
mock factories on ``runner.SCRAPER_CLASSES``, and the Firestore helpers
and LLM pipeline are monkeypatched on the runner module. These tests
verify orchestration only — sequencing, dedup, saving, health logging,
crash isolation, and the pipeline hand-off — not scraper behavior.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.settings import MAX_ARTICLES_PER_SOURCE
from scrapers import runner
from scrapers.sources._36kr import ThirtySixKrScraper
from scrapers.sources.autohome import AutohomeScraper
from scrapers.sources.baidu_news import BaiduNewsScraper
from scrapers.sources.cnevpost import CnEVPostScraper
from scrapers.sources.gasgoo import GasgooScraper

SCRAPED_ARTICLE = {
    "source_name": "source_a",
    "source_url": "https://example.com/source_a/1",
    "title": "BYD Launches City NOA",
    "body": "BYD announced today...",
    "publish_date": "2026-07-15T08:00:00+00:00",
    "scrape_date": "2026-07-18T00:00:00+00:00",
    "language": "en",
    "raw_html": "<html></html>",
}

PIPELINE_SUMMARY = {"total": 2, "succeeded": 2, "failed": 0}


def _entries(source_name: str, count: int) -> list[dict[str, str]]:
    """Build ``count`` discovery entries with unique URLs for one source."""
    return [
        {
            "url": f"https://example.com/{source_name}/{i}",
            "title": f"Article {i}",
            "publish_date": "",
        }
        for i in range(count)
    ]


def _health(source_name: str, error_count: int = 0) -> dict[str, Any]:
    """Build a health dict shaped like ``BaseScraper._get_health_metrics()``."""
    return {
        "source_name": source_name,
        "requests_made": 3,
        "error_count": error_count,
        "errors": [{"url": "https://example.com/bad", "status_code": 500, "message": "boom"}]
        * error_count,
        "duration_seconds": 1.5,
    }


def _make_scraper(
    source_name: str = "source_a",
    discovered: list[dict[str, str]] | None = None,
    scraped: dict[str, str] | None = None,
    error_count: int = 0,
) -> MagicMock:
    """Build a mock source scraper usable as an async context manager."""
    scraper = MagicMock()
    scraper.SOURCE_NAME = source_name
    scraper.discover_articles = AsyncMock(
        return_value=discovered if discovered is not None else _entries(source_name, 1)
    )
    scraper.scrape_article = AsyncMock(
        return_value=scraped if scraped is not None else dict(SCRAPED_ARTICLE)
    )
    scraper._get_health_metrics = MagicMock(return_value=_health(source_name, error_count))
    scraper.reset_health_metrics = MagicMock()
    scraper.__aenter__.return_value = scraper
    scraper.__aexit__.return_value = False
    return scraper


def _install_scrapers(monkeypatch: pytest.MonkeyPatch, scrapers: list[MagicMock]) -> None:
    """Replace the runner's scraper registry with mock scraper classes.

    The module-level SCRAPER_CLASSES list captures the real classes at
    import time, so the list itself is replaced rather than the imported
    class names.
    """
    classes = []
    for scraper in scrapers:
        scraper_class = MagicMock(return_value=scraper)
        scraper_class.SOURCE_NAME = scraper.SOURCE_NAME
        classes.append(scraper_class)
    monkeypatch.setattr(runner, "SCRAPER_CLASSES", classes)


def _default_fleet() -> list[MagicMock]:
    """Build four healthy mock scrapers, one discovered article each."""
    return [
        _make_scraper(source_name=name) for name in ("source_a", "source_b", "source_c", "source_d")
    ]


def _saved_health(io_mocks: dict[str, AsyncMock], index: int = 0) -> dict[str, Any]:
    """Return the metrics dict passed to the ``index``-th save_health_metrics call."""
    saved: dict[str, Any] = io_mocks["save_health_metrics"].await_args_list[index].args[0]
    return saved


@pytest.fixture
def io_mocks(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Mock the Firestore helpers and the LLM pipeline on the runner module."""
    mocks = {
        "article_exists": AsyncMock(return_value=False),
        "save_article": AsyncMock(return_value="doc-1"),
        "save_health_metrics": AsyncMock(return_value="health-1"),
        "run_pipeline": AsyncMock(return_value=dict(PIPELINE_SUMMARY)),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(runner, name, mock)
    return mocks


class TestScraperRegistry:
    """The real scraper registry, before any mocking."""

    def test_scraper_classes_registered_in_build_order(self) -> None:
        """All registered scrapers appear in the spec's build order."""
        assert runner.SCRAPER_CLASSES == [
            GasgooScraper,
            CnEVPostScraper,
            BaiduNewsScraper,
            AutohomeScraper,
            ThirtySixKrScraper,
        ]


class TestRunAllScrapers:
    """run_all_scrapers orchestrates scraping, dedup, health, and the pipeline."""

    async def test_run_all_scrapers_runs_all_sources(
        self, monkeypatch: pytest.MonkeyPatch, io_mocks: dict[str, AsyncMock]
    ) -> None:
        """Every registered scraper is reset and asked to discover articles."""
        scrapers = _default_fleet()
        _install_scrapers(monkeypatch, scrapers)

        await runner.run_all_scrapers(run_pipeline_after=False)

        for scraper in scrapers:
            scraper.reset_health_metrics.assert_called_once()
            scraper.discover_articles.assert_awaited_once()

    async def test_run_all_scrapers_saves_new_articles(
        self, monkeypatch: pytest.MonkeyPatch, io_mocks: dict[str, AsyncMock]
    ) -> None:
        """Each new discovered article is scraped and saved."""
        scraper = _make_scraper(discovered=_entries("source_a", 3))
        _install_scrapers(monkeypatch, [scraper])

        result = await runner.run_all_scrapers(run_pipeline_after=False)

        assert scraper.scrape_article.await_count == 3
        assert io_mocks["save_article"].await_count == 3
        io_mocks["save_article"].assert_awaited_with(SCRAPED_ARTICLE)
        assert result["total_articles_ingested"] == 3

    async def test_run_all_scrapers_skips_existing_articles(
        self, monkeypatch: pytest.MonkeyPatch, io_mocks: dict[str, AsyncMock]
    ) -> None:
        """URLs already stored in Firestore are never scraped (dedup first)."""
        io_mocks["article_exists"].return_value = True
        scraper = _make_scraper(discovered=_entries("source_a", 2))
        _install_scrapers(monkeypatch, [scraper])

        result = await runner.run_all_scrapers(run_pipeline_after=False)

        scraper.scrape_article.assert_not_awaited()
        io_mocks["save_article"].assert_not_awaited()
        assert result["total_articles_ingested"] == 0

    async def test_run_all_scrapers_saves_health_metrics(
        self, monkeypatch: pytest.MonkeyPatch, io_mocks: dict[str, AsyncMock]
    ) -> None:
        """Health metrics are saved once per source."""
        _install_scrapers(monkeypatch, _default_fleet())

        await runner.run_all_scrapers(run_pipeline_after=False)

        assert io_mocks["save_health_metrics"].await_count == 4

    async def test_run_all_scrapers_health_status_success(
        self, monkeypatch: pytest.MonkeyPatch, io_mocks: dict[str, AsyncMock]
    ) -> None:
        """Zero errors yields a "success" health status."""
        scraper = _make_scraper(discovered=_entries("source_a", 1), error_count=0)
        _install_scrapers(monkeypatch, [scraper])

        result = await runner.run_all_scrapers(run_pipeline_after=False)

        saved = _saved_health(io_mocks)
        assert saved["status"] == "success"
        assert saved["articles_ingested"] == 1
        assert result["source_results"][0]["status"] == "success"

    async def test_run_all_scrapers_health_status_partial(
        self, monkeypatch: pytest.MonkeyPatch, io_mocks: dict[str, AsyncMock]
    ) -> None:
        """Errors alongside ingested articles yield a "partial" health status."""
        scraper = _make_scraper(discovered=_entries("source_a", 1), error_count=2)
        _install_scrapers(monkeypatch, [scraper])

        result = await runner.run_all_scrapers(run_pipeline_after=False)

        saved = _saved_health(io_mocks)
        assert saved["status"] == "partial"
        assert saved["articles_ingested"] == 1
        assert result["source_results"][0]["status"] == "partial"

    async def test_run_all_scrapers_health_status_failure(
        self, monkeypatch: pytest.MonkeyPatch, io_mocks: dict[str, AsyncMock]
    ) -> None:
        """Errors with zero ingested articles yield a "failure" health status."""
        scraper = _make_scraper(discovered=_entries("source_a", 2), scraped={}, error_count=3)
        _install_scrapers(monkeypatch, [scraper])

        result = await runner.run_all_scrapers(run_pipeline_after=False)

        saved = _saved_health(io_mocks)
        assert saved["status"] == "failure"
        assert saved["articles_ingested"] == 0
        assert result["source_results"][0]["status"] == "failure"

    async def test_run_all_scrapers_continues_on_scraper_crash(
        self, monkeypatch: pytest.MonkeyPatch, io_mocks: dict[str, AsyncMock]
    ) -> None:
        """A crashed source is recorded as "failure" and the rest still run."""
        crashed = _make_scraper(source_name="crash_source")
        crashed.discover_articles = AsyncMock(side_effect=RuntimeError("browser exploded"))
        healthy = _default_fleet()[:3]
        _install_scrapers(monkeypatch, [crashed, *healthy])

        result = await runner.run_all_scrapers(run_pipeline_after=False)

        for scraper in healthy:
            scraper.discover_articles.assert_awaited_once()
        assert io_mocks["save_health_metrics"].await_count == 4
        assert _saved_health(io_mocks, index=0)["status"] == "failure"
        assert result["sources_run"] == 4
        assert result["source_results"][0]["status"] == "failure"

    async def test_run_all_scrapers_runs_pipeline(
        self, monkeypatch: pytest.MonkeyPatch, io_mocks: dict[str, AsyncMock]
    ) -> None:
        """The LLM pipeline runs after scraping when run_pipeline_after is True."""
        _install_scrapers(monkeypatch, [_make_scraper()])

        result = await runner.run_all_scrapers(run_pipeline_after=True)

        io_mocks["run_pipeline"].assert_awaited_once()
        assert result["pipeline_result"] == PIPELINE_SUMMARY

    async def test_run_all_scrapers_skips_pipeline(
        self, monkeypatch: pytest.MonkeyPatch, io_mocks: dict[str, AsyncMock]
    ) -> None:
        """The LLM pipeline is skipped when run_pipeline_after is False."""
        _install_scrapers(monkeypatch, [_make_scraper()])

        result = await runner.run_all_scrapers(run_pipeline_after=False)

        io_mocks["run_pipeline"].assert_not_awaited()
        assert result["pipeline_result"] is None

    async def test_run_all_scrapers_returns_summary(
        self, monkeypatch: pytest.MonkeyPatch, io_mocks: dict[str, AsyncMock]
    ) -> None:
        """The summary reports totals and one result entry per source."""
        _install_scrapers(monkeypatch, _default_fleet())

        result = await runner.run_all_scrapers()

        assert result["sources_run"] == 4
        assert result["total_articles_ingested"] == 4
        assert result["total_errors"] == 0
        assert result["max_articles_per_source"] == MAX_ARTICLES_PER_SOURCE
        assert len(result["source_results"]) == 4
        for entry in result["source_results"]:
            assert set(entry) == {"source_name", "status", "articles_ingested", "error_count"}
            assert isinstance(entry["source_name"], str)
            assert isinstance(entry["status"], str)
            assert isinstance(entry["articles_ingested"], int)
            assert isinstance(entry["error_count"], int)
        assert result["pipeline_result"] == PIPELINE_SUMMARY

    async def test_run_all_scrapers_caps_articles_per_source(
        self, monkeypatch: pytest.MonkeyPatch, io_mocks: dict[str, AsyncMock]
    ) -> None:
        """Only max_articles_per_source new articles are scraped per source."""
        scraper = _make_scraper(discovered=_entries("source_a", 10))
        _install_scrapers(monkeypatch, [scraper])

        result = await runner.run_all_scrapers(run_pipeline_after=False, max_articles_per_source=3)

        assert scraper.scrape_article.await_count == 3
        assert io_mocks["save_article"].await_count == 3
        assert result["total_articles_ingested"] == 3
        assert result["max_articles_per_source"] == 3

    async def test_run_all_scrapers_cap_does_not_count_dupes(
        self, monkeypatch: pytest.MonkeyPatch, io_mocks: dict[str, AsyncMock]
    ) -> None:
        """Duplicate articles (already in Firestore) do not count toward the cap."""
        entries = _entries("source_a", 5)
        scraper = _make_scraper(discovered=entries)
        _install_scrapers(monkeypatch, [scraper])
        # First 2 URLs are already stored; the next 3 are new. Cap = 2.
        existing_urls = {entries[0]["url"], entries[1]["url"]}
        io_mocks["article_exists"].side_effect = lambda url: url in existing_urls

        result = await runner.run_all_scrapers(run_pipeline_after=False, max_articles_per_source=2)

        assert scraper.scrape_article.await_count == 2
        assert io_mocks["save_article"].await_count == 2
        assert result["total_articles_ingested"] == 2

    async def test_run_all_scrapers_cap_in_summary(
        self, monkeypatch: pytest.MonkeyPatch, io_mocks: dict[str, AsyncMock]
    ) -> None:
        """The effective cap is included in the run summary."""
        _install_scrapers(monkeypatch, [_make_scraper()])

        result = await runner.run_all_scrapers(run_pipeline_after=False)

        assert "max_articles_per_source" in result
        assert isinstance(result["max_articles_per_source"], int)

    async def test_run_all_scrapers_handles_empty_discovery(
        self, monkeypatch: pytest.MonkeyPatch, io_mocks: dict[str, AsyncMock]
    ) -> None:
        """A source discovering nothing scrapes nothing but still logs health."""
        scraper = _make_scraper(discovered=[])
        _install_scrapers(monkeypatch, [scraper])

        await runner.run_all_scrapers(run_pipeline_after=False)

        scraper.scrape_article.assert_not_awaited()
        io_mocks["save_health_metrics"].assert_awaited_once()
        assert _saved_health(io_mocks)["articles_ingested"] == 0

    async def test_run_all_scrapers_skips_empty_scrape_results(
        self, monkeypatch: pytest.MonkeyPatch, io_mocks: dict[str, AsyncMock]
    ) -> None:
        """An empty scrape result is not saved; the others still are."""
        scraper = _make_scraper(discovered=_entries("source_a", 2))
        scraper.scrape_article = AsyncMock(side_effect=[{}, dict(SCRAPED_ARTICLE)])
        _install_scrapers(monkeypatch, [scraper])

        result = await runner.run_all_scrapers(run_pipeline_after=False)

        io_mocks["save_article"].assert_awaited_once_with(SCRAPED_ARTICLE)
        assert result["total_articles_ingested"] == 1
