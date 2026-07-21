"""Scraper runner: the cron entrypoint for the data pipeline.

Runs every registered source scraper sequentially (never parallel), dedupes
discovered URLs against Firestore before scraping, writes new articles to
the ``articles`` collection, records per-source health metrics in the
``scraper_health`` collection, and optionally triggers the LLM extraction
pipeline afterwards. A failure in one source never stops the run.

Cron usage: ``cd /path/to/backend && python -m scrapers.runner``
"""

from __future__ import annotations

import json
import logging
from typing import Any

from config.settings import MAX_ARTICLES_PER_SOURCE
from db.firestore import article_exists, save_article, save_health_metrics
from processing.pipeline import run_pipeline
from scrapers.dynamic import DynamicScraper
from scrapers.sources._36kr import ThirtySixKrScraper
from scrapers.sources.autohome import AutohomeScraper
from scrapers.sources.baidu_news import BaiduNewsScraper
from scrapers.sources.cnevpost import CnEVPostScraper
from scrapers.sources.gasgoo import GasgooScraper
from scrapers.static import StaticScraper

logger = logging.getLogger(__name__)

# Source scrapers are async context managers; BaseScraper itself is not,
# so the union of the two concrete branches is the accurate element type.
SourceScraper = StaticScraper | DynamicScraper

SCRAPER_CLASSES: list[type[SourceScraper]] = [
    GasgooScraper,
    CnEVPostScraper,
    BaiduNewsScraper,
    AutohomeScraper,
    ThirtySixKrScraper,
]


def _empty_metrics(source_name: str) -> dict[str, Any]:
    """Return zeroed health metrics for a scraper that never got started.

    dict[str, Any]: health metric values are heterogeneous (str, int,
    list, float), matching BaseScraper's HealthMetrics shape.
    """
    return {
        "source_name": source_name,
        "requests_made": 0,
        "error_count": 0,
        "errors": [],
        "duration_seconds": 0.0,
    }


def _determine_status(error_count: int, articles_ingested: int) -> str:
    """Map error and ingest counts to a ``scraper_health`` status value."""
    if error_count == 0:
        return "success"
    if articles_ingested > 0:
        return "partial"
    return "failure"


async def _ingest_new_articles(scraper: SourceScraper, max_articles: int) -> int:
    """Discover, dedupe, scrape, and save articles for one source.

    Returns the number of new articles written to Firestore, capped at
    ``max_articles`` per run; already-stored URLs are skipped before
    scraping (saving requests) and do not count toward the cap. Empty
    scrape results are skipped without saving (the scraper already
    logged why).
    """
    source_name = scraper.SOURCE_NAME
    discovered = await scraper.discover_articles()
    articles_ingested = 0
    for i, entry in enumerate(discovered):
        url = entry["url"]
        if await article_exists(url):
            logger.debug(f"[{source_name}] skipping already-stored article {url}")
            continue
        article = await scraper.scrape_article(url)
        if not article:
            continue
        await save_article(article)
        articles_ingested += 1
        if articles_ingested >= max_articles:
            logger.info(
                f"[{source_name}] reached cap of {max_articles} articles, "
                f"stopping ({len(discovered) - i - 1} remaining)"
            )
            break
    return articles_ingested


async def _run_source(scraper_class: type[SourceScraper], max_articles: int) -> dict[str, Any]:
    """Run one source scraper end to end and record its health metrics.

    Never raises: a crash anywhere in the source's run is logged, recorded
    as a "failure" health document, and the runner moves on to the next
    source. Returns this source's entry for the run summary.

    dict[str, Any]: summary values are heterogeneous (str status, int counts).
    """
    source_name = scraper_class.SOURCE_NAME
    logger.info(f"Starting {source_name}")
    scraper: SourceScraper | None = None
    articles_ingested = 0
    crashed = False
    try:
        scraper = scraper_class()
        scraper.reset_health_metrics()
        async with scraper:
            articles_ingested = await _ingest_new_articles(scraper, max_articles)
    except Exception:  # one source must never crash the whole run
        logger.exception(f"[{source_name}] scraper run crashed")
        crashed = True
    metrics: dict[str, Any] = (
        dict(scraper._get_health_metrics()) if scraper is not None else _empty_metrics(source_name)
    )
    error_count = int(metrics["error_count"])
    status = "failure" if crashed else _determine_status(error_count, articles_ingested)
    metrics["articles_ingested"] = articles_ingested
    metrics["status"] = status
    try:
        await save_health_metrics(metrics)
    except Exception:  # a reporting failure must not crash the run either
        logger.exception(f"[{source_name}] failed to save health metrics")
    logger.info(f"Finished {source_name}: {articles_ingested} articles, {error_count} errors")
    return {
        "source_name": source_name,
        "status": status,
        "articles_ingested": articles_ingested,
        "error_count": error_count,
    }


async def run_all_scrapers(
    run_pipeline_after: bool = True,
    max_articles_per_source: int | None = None,
) -> dict[str, Any]:
    """Run every registered scraper sequentially and return a run summary.

    The cron entrypoint flow from docs/scraper-spec.md: for each source,
    discover articles, dedupe against Firestore by URL, scrape and save
    the new ones (at most ``max_articles_per_source`` per source, falling
    back to the ``MAX_ARTICLES_PER_SOURCE`` config default when None),
    and record health metrics. Afterwards, optionally run the LLM
    extraction pipeline over the newly ingested articles.

    dict[str, Any]: the summary mixes counts, per-source dicts, and the
    optional pipeline result (None when ``run_pipeline_after`` is False).
    """
    effective_cap = (
        max_articles_per_source if max_articles_per_source is not None else MAX_ARTICLES_PER_SOURCE
    )
    source_results: list[dict[str, Any]] = []
    for scraper_class in SCRAPER_CLASSES:
        source_results.append(await _run_source(scraper_class, effective_cap))

    pipeline_result: dict[str, int] | None = None
    if run_pipeline_after:
        logger.info("Starting LLM extraction pipeline")
        pipeline_result = await run_pipeline()
        logger.info(
            f"Pipeline finished: total={pipeline_result['total']} "
            f"succeeded={pipeline_result['succeeded']} failed={pipeline_result['failed']}"
        )

    return {
        "sources_run": len(source_results),
        "total_articles_ingested": sum(result["articles_ingested"] for result in source_results),
        "total_errors": sum(result["error_count"] for result in source_results),
        "max_articles_per_source": effective_cap,
        "source_results": source_results,
        "pipeline_result": pipeline_result,
    }


if __name__ == "__main__":
    import asyncio

    result = asyncio.run(run_all_scrapers())
    # stdout is the cron job's output channel; logging stays on stderr.
    print(json.dumps(result, indent=2))
