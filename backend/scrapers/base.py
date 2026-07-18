"""Abstract base class for all source scrapers.

Provides the request behavior shared by every scraper: randomized rate
limiting, User-Agent rotation, retry with exponential backoff, request
logging, and health metrics for the ``scraper_health`` collection.
Subclasses (StaticScraper, DynamicScraper) supply the actual HTTP or
browser fetch functions.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, TypedDict

from config.settings import MAX_RETRIES, SCRAPE_DELAY_MAX, SCRAPE_DELAY_MIN

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class ErrorRecord(TypedDict):
    """One failed request, shaped for `scraper_health.errors`."""

    url: str
    status_code: int | None
    message: str


class HealthMetrics(TypedDict):
    """Run summary, shaped for the `scraper_health` collection."""

    source_name: str
    requests_made: int
    error_count: int
    errors: list[ErrorRecord]
    duration_seconds: float


class BaseScraper(ABC):
    """Base class for all source scrapers.

    Subclasses must define ``SOURCE_NAME`` and ``BASE_URL`` and implement
    ``discover_articles`` and ``scrape_article``. Never subclass this
    directly in ``scrapers/sources/``; extend StaticScraper or
    DynamicScraper instead.
    """

    SOURCE_NAME: str
    BASE_URL: str

    _USER_AGENTS: ClassVar[list[str]] = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) Gecko/20100101 Firefox/146.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:145.0) Gecko/20100101 Firefox/145.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:146.0) Gecko/20100101 Firefox/146.0",
        "Mozilla/5.0 (X11; Linux x86_64; rv:145.0) Gecko/20100101 Firefox/145.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/26.2 Safari/605.1.15",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/26.1 Safari/605.1.15",
    ]

    def __init__(self) -> None:
        """Set up logging and zeroed health metrics."""
        self.logger = logging.getLogger(__name__)
        self.requests_made = 0
        self.error_count = 0
        self.errors: list[ErrorRecord] = []
        self.start_time = time.time()

    async def _delay(self) -> None:
        """Sleep a random interval to rate-limit requests."""
        # S311: crawl-politeness jitter, not a cryptographic use of randomness.
        delay = random.uniform(SCRAPE_DELAY_MIN, SCRAPE_DELAY_MAX)  # noqa: S311
        await asyncio.sleep(delay)

    def _get_random_ua(self) -> str:
        """Return a random User-Agent string from the pool."""
        # S311: UA rotation, not a cryptographic use of randomness.
        return random.choice(self._USER_AGENTS)  # noqa: S311

    async def _request_with_retry(
        self,
        fetch_func: Callable[..., Awaitable[Any]],
        url: str,
        **kwargs: Any,
    ) -> Any:
        """Call ``fetch_func(url, **kwargs)`` with rate limiting and retries.

        Retries up to MAX_RETRIES attempts with exponential backoff.
        Returns the fetch result, or None once all attempts fail. Never
        raises: a single bad URL must not crash the runner.

        Any is required here: the result type is whatever the injected
        fetch function yields (httpx.Response, rendered HTML str, ...),
        and kwargs pass through to it verbatim.
        """
        for attempt in range(MAX_RETRIES):
            await self._delay()
            try:
                result = await fetch_func(url, **kwargs)
            except Exception as exc:  # never crash the runner on a single request
                self._record_error(url, exc, attempt)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2**attempt)
            else:
                self.requests_made += 1
                self._log_request(url, result)
                return result
        self.logger.error(f"[{self.SOURCE_NAME}] giving up on {url} after {MAX_RETRIES} attempts")
        return None

    def _log_request(self, url: str, result: object) -> None:
        """Log URL, status code, response size, and timestamp for one request."""
        status_code: int | None = getattr(result, "status_code", None)
        content = result if isinstance(result, bytes | str) else getattr(result, "content", None)
        size = len(content) if isinstance(content, bytes | str) else None
        timestamp = datetime.now(tz=UTC).isoformat()
        self.logger.info(
            f"[{self.SOURCE_NAME}] fetched url={url} status={status_code} "
            f"size={size} at={timestamp}"
        )

    def _record_error(self, url: str, exc: Exception, attempt: int) -> None:
        """Count and log one failed request attempt."""
        response: object = getattr(exc, "response", None)
        status_code: int | None = getattr(response, "status_code", None)
        message = f"{type(exc).__name__}: {exc}"
        self.error_count += 1
        self.errors.append({"url": url, "status_code": status_code, "message": message})
        self.logger.warning(
            f"[{self.SOURCE_NAME}] attempt {attempt + 1}/{MAX_RETRIES} failed for {url}: {message}"
        )

    def _get_health_metrics(self) -> HealthMetrics:
        """Return this run's health metrics for the `scraper_health` collection."""
        return {
            "source_name": self.SOURCE_NAME,
            "requests_made": self.requests_made,
            "error_count": self.error_count,
            "errors": list(self.errors),
            "duration_seconds": time.time() - self.start_time,
        }

    def reset_health_metrics(self) -> None:
        """Zero all counters, clear errors, and restart the run timer."""
        self.requests_made = 0
        self.error_count = 0
        self.errors = []
        self.start_time = time.time()

    @abstractmethod
    async def discover_articles(self) -> list[dict[str, str]]:
        """Find new article URLs to scrape from listing/index pages.

        Returns a list of dicts with keys: ``url``, ``title``,
        ``publish_date`` (ISO 8601), where available from the listing.
        """

    @abstractmethod
    async def scrape_article(self, url: str) -> dict[str, str]:
        """Scrape a single article page and extract its content.

        Returns a dict with keys: ``source_name``, ``source_url``,
        ``title``, ``body``, ``publish_date``, ``scrape_date``,
        ``language`` ("zh" or "en"), and ``raw_html``.
        """
