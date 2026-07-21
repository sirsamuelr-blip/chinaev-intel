"""Scraper runner: the cron entrypoint for the data pipeline.

Runs every registered source scraper sequentially (never parallel), dedupes
discovered URLs against Firestore before scraping, writes new articles to
the ``articles`` collection, records per-source health metrics in the
``scraper_health`` collection, and optionally triggers the LLM extraction
pipeline afterwards. A failure in one source never stops the run.

The opt-in Phase 2 intelligence flow (``run_phase2_after`` /
``--phase2``) then runs entity promotion, cross-source deduplication,
signal detection, and novelty scoring over recently processed articles.

Cron usage: ``cd /path/to/backend && python -m scrapers.runner``
Phase 2 opt-in: ``python -m scrapers.runner --phase2``
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from config.settings import MAX_ARTICLES_PER_SOURCE
from db.firestore import (
    article_exists,
    get_recent_processed_articles,
    get_recent_signals,
    keys_to_camel,
    save_article,
    save_health_metrics,
)
from processing.dedup import deduplicate_articles, mark_duplicates
from processing.entities import promote_entities_from_article
from processing.novelty import score_article_batch, score_signal_batch
from processing.pipeline import run_pipeline
from processing.signals import detect_signals_from_articles
from scrapers.dynamic import DynamicScraper
from scrapers.sources._36kr import ThirtySixKrScraper
from scrapers.sources.autohome import AutohomeScraper
from scrapers.sources.baidu_news import BaiduNewsScraper
from scrapers.sources.cnevpost import CnEVPostScraper
from scrapers.sources.dongchedi import DongchediScraper
from scrapers.sources.gasgoo import GasgooScraper
from scrapers.sources.xiaohongshu import XiaoHongShuScraper
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
    DongchediScraper,
    XiaoHongShuScraper,
]

# Phase 2 operates on articles processed within this window: wide enough to
# cover articles from the previous run whose Phase 2 pass failed, without
# re-chewing the whole recent corpus every 6-hour cron cycle.
PHASE2_LOOKBACK_HOURS = 24

# Signals saved within this window count as "newly generated" for novelty
# scoring; detect_signals_from_articles persists signals without returning
# them, so the runner re-reads them from Firestore.
NEW_SIGNAL_LOOKBACK_DAYS = 1


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


async def _phase2_promote_entities(articles: list[dict[str, Any]]) -> dict[str, int]:
    """Promote brands, vehicles, and features from each article.

    A single article's promotion failure is logged and skipped — the rest
    of the batch still runs. Returns the summed promotion counts.
    """
    totals = {
        "articles_processed": 0,
        "brands_promoted": 0,
        "vehicles_promoted": 0,
        "features_promoted": 0,
    }
    for article in articles:
        try:
            counts = await promote_entities_from_article(article)
        except Exception:
            logger.exception(f"entity promotion failed article_id={article.get('id')}")
            continue
        totals["articles_processed"] += 1
        for key in ("brands_promoted", "vehicles_promoted", "features_promoted"):
            totals[key] += counts.get(key, 0)
    return totals


async def _phase2_dedup(articles: list[dict[str, Any]]) -> dict[str, Any]:
    """Find cross-source duplicate groups and persist the dedup fields.

    Articles newly marked duplicate also get ``isDuplicate=True`` set on
    the in-memory dicts, so the signal detection step sees this run's
    dedup results without re-fetching from Firestore.

    dict[str, Any]: mixes int counts from analysis and persistence.
    """
    result = await deduplicate_articles(articles)
    updated = await mark_duplicates(result["groups"])
    duplicate_ids = {str(dup_id) for group in result["groups"] for dup_id in group["duplicate_ids"]}
    for article in articles:
        if str(article.get("id")) in duplicate_ids:
            article["isDuplicate"] = True
    return {
        "duplicate_groups_found": result["duplicate_groups_found"],
        "articles_marked_duplicate": result["articles_marked_duplicate"],
        "documents_updated": updated,
    }


async def _phase2_detect_signals(articles: list[dict[str, Any]]) -> dict[str, Any]:
    """Run signal detection over the non-duplicate articles.

    dict[str, Any]: the detection summary mixes counts and a per-type map.
    """
    non_duplicates = [article for article in articles if not article.get("isDuplicate")]
    return await detect_signals_from_articles(non_duplicates)


async def _phase2_score_novelty(articles: list[dict[str, Any]]) -> dict[str, Any]:
    """Score novelty for the article batch and any newly generated signals.

    Scores are logged only — persisting them is Phase 3 (admin dashboard)
    territory.

    dict[str, Any]: count values, kept Any-typed to match the other steps.
    """
    article_scores = await score_article_batch(articles)
    new_signals = await get_recent_signals(days=NEW_SIGNAL_LOOKBACK_DAYS)
    signal_scores = await score_signal_batch(new_signals)
    for entry in article_scores:
        logger.info(
            f"article novelty article_id={entry['article_id']} score={entry['novelty_score']:.2f}"
        )
    for entry in signal_scores:
        logger.info(
            f"signal novelty signal_id={entry['signal_id']} score={entry['novelty_score']:.2f}"
        )
    return {"articles_scored": len(article_scores), "signals_scored": len(signal_scores)}


async def run_phase2_processing(lookback_hours: int = PHASE2_LOOKBACK_HOURS) -> dict[str, Any]:
    """Run the Phase 2 intelligence pipeline over recently processed articles.

    Flow: entity promotion -> deduplication -> signal detection -> novelty
    scoring. Articles come back snake_case from the db layer and are
    bridged to camelCase once up front — the Phase 2 modules read the
    Firestore doc shape (``id`` passes through unchanged). Each step is
    independently guarded: a failing step is logged, recorded under
    ``errors``, and never blocks the steps after it.

    dict[str, Any]: the summary mixes counts and per-step result dicts
    (None for steps that failed or never ran).
    """
    summary: dict[str, Any] = {
        "articles_fetched": 0,
        "entity_promotion": None,
        "dedup": None,
        "signals": None,
        "novelty": None,
        "errors": [],
    }
    try:
        fetched = await get_recent_processed_articles(hours=lookback_hours)
    except Exception:
        logger.exception("phase 2 aborted: failed to fetch recently processed articles")
        summary["errors"].append("fetch_articles")
        return summary
    articles = [keys_to_camel(article) for article in fetched]
    summary["articles_fetched"] = len(articles)
    logger.info(f"phase 2 starting: {len(articles)} articles from last {lookback_hours}h")

    steps: list[tuple[str, Callable[[list[dict[str, Any]]], Awaitable[dict[str, Any]]]]] = [
        ("entity_promotion", _phase2_promote_entities),
        ("dedup", _phase2_dedup),
        ("signals", _phase2_detect_signals),
        ("novelty", _phase2_score_novelty),
    ]
    for step_name, step in steps:
        logger.info(f"phase 2 step starting: {step_name}")
        try:
            summary[step_name] = await step(articles)
        except Exception:  # one step failing must not block the next
            logger.exception(f"phase 2 step failed: {step_name}")
            summary["errors"].append(step_name)
            continue
        logger.info(f"phase 2 step complete: {step_name} result={summary[step_name]}")
    return summary


async def run_all_scrapers(
    run_pipeline_after: bool = True,
    max_articles_per_source: int | None = None,
    run_phase2_after: bool = False,
) -> dict[str, Any]:
    """Run every registered scraper sequentially and return a run summary.

    The cron entrypoint flow from docs/scraper-spec.md: for each source,
    discover articles, dedupe against Firestore by URL, scrape and save
    the new ones (at most ``max_articles_per_source`` per source, falling
    back to the ``MAX_ARTICLES_PER_SOURCE`` config default when None),
    and record health metrics. Afterwards, optionally run the LLM
    extraction pipeline over the newly ingested articles, then optionally
    (``run_phase2_after``, off by default so the existing cron behavior
    is unchanged) the Phase 2 intelligence flow via
    ``run_phase2_processing``.

    dict[str, Any]: the summary mixes counts, per-source dicts, and the
    optional pipeline/phase-2 results (None when their flags are False).
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

    phase2_result: dict[str, Any] | None = None
    if run_phase2_after:
        logger.info("Starting Phase 2 intelligence pipeline")
        phase2_result = await run_phase2_processing()
        logger.info(
            f"Phase 2 finished: articles_fetched={phase2_result['articles_fetched']} "
            f"failed_steps={phase2_result['errors']}"
        )

    return {
        "sources_run": len(source_results),
        "total_articles_ingested": sum(result["articles_ingested"] for result in source_results),
        "total_errors": sum(result["error_count"] for result in source_results),
        "max_articles_per_source": effective_cap,
        "source_results": source_results,
        "pipeline_result": pipeline_result,
        "phase2_result": phase2_result,
    }


if __name__ == "__main__":
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Run all source scrapers (cron entrypoint)")
    parser.add_argument(
        "--phase2",
        action="store_true",
        help="after the LLM pipeline, run Phase 2 processing "
        "(entity promotion, dedup, signal detection, novelty scoring)",
    )
    args = parser.parse_args()
    result = asyncio.run(run_all_scrapers(run_phase2_after=args.phase2))
    # stdout is the cron job's output channel; logging stays on stderr.
    print(json.dumps(result, indent=2))
