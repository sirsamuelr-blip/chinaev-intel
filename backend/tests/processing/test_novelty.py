"""Tests for the novelty scoring module.

The scoring functions are pure and tested directly with dict fixtures —
camelCase keys for articles (Firestore doc shape), snake_case keys for
signals (db layer convention). The batch entry points monkeypatch the
Firestore fetchers on the novelty module (matching the ``test_dedup``
pattern) — no real Firestore client is touched.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from processing import novelty


def _make_article(**overrides: Any) -> dict[str, Any]:
    """Build a camelCase article doc (Firestore shape) with overridable fields."""
    article: dict[str, Any] = {
        "id": "art-a",
        "titleEn": "BYD launches God's Eye ADAS on the Seal",
        "contentType": "news",
        "brandsMentioned": ["BYD"],
        "vehiclesMentioned": ["BYD Seal"],
        "featuresExtracted": [{"feature_name": "God's Eye", "category": "adas"}],
        "duplicateGroupId": None,
    }
    article.update(overrides)
    return article


def _make_nio_article(**overrides: Any) -> dict[str, Any]:
    """Build an article about an unrelated NIO story (no overlap with the base)."""
    article = _make_article(
        titleEn="NIO opens its 3000th battery swap station nationwide",
        contentType="review",
        brandsMentioned=["NIO"],
        vehiclesMentioned=["NIO ET5"],
        featuresExtracted=[{"feature_name": "Swap 4.0", "category": "battery_software"}],
    )
    article.update(overrides)
    return article


def _make_signal(**overrides: Any) -> dict[str, Any]:
    """Build a snake_case signal dict (db layer shape) with overridable fields."""
    signal: dict[str, Any] = {
        "id": "sig-a",
        "signal_type": "new_feature_launch",
        "brands_mentioned": ["BYD"],
        "features_mentioned": ["God's Eye"],
        "source_article_ids": ["art-a"],
    }
    signal.update(overrides)
    return signal


class TestScoreArticleNovelty:
    """score_article_novelty inverts the max similarity to recent articles."""

    def test_identical_article_returns_low_novelty(self) -> None:
        """A recent article with identical fields drives novelty to 0.0."""
        recent = _make_article(id="art-old")

        score = novelty.score_article_novelty(_make_article(), [recent])

        assert score == pytest.approx(0.0)

    def test_completely_different_returns_high_novelty(self) -> None:
        """An unrelated recent article leaves the score near fully novel."""
        recent = _make_nio_article(id="art-old")

        score = novelty.score_article_novelty(_make_article(), [recent])

        assert score > 0.7

    def test_empty_recent_returns_1(self) -> None:
        """No recent articles means fully novel by definition."""
        assert novelty.score_article_novelty(_make_article(), []) == 1.0

    def test_skips_same_id(self) -> None:
        """The article never compares against itself."""
        assert novelty.score_article_novelty(_make_article(), [_make_article()]) == 1.0

    def test_skips_same_duplicate_group(self) -> None:
        """Members of the article's own duplicate group are dedup's territory."""
        article = _make_article(duplicateGroupId="group-1")
        recent = _make_article(id="art-old", duplicateGroupId="group-1")

        assert novelty.score_article_novelty(article, [recent]) == 1.0

    def test_same_brand_different_topic_moderate_novelty(self) -> None:
        """Brand overlap alone dents the score without flooring it."""
        recent = _make_article(
            id="art-old",
            titleEn="BYD quarterly earnings beat analyst expectations",
            contentType="earnings",
            vehiclesMentioned=[],
            featuresExtracted=[],
        )

        score = novelty.score_article_novelty(_make_article(), [recent])

        assert 0.5 < score <= 0.75

    def test_same_content_type_same_brand_penalty(self) -> None:
        """A contentType match on the same primary brand costs the 0.15 penalty."""
        article = _make_article()
        same_type = _make_article(
            id="art-old",
            titleEn="BYD cuts prices across its lineup",
            vehiclesMentioned=[],
            featuresExtracted=[],
        )
        different_type = _make_article(
            id="art-old",
            titleEn="BYD cuts prices across its lineup",
            contentType="opinion",
            vehiclesMentioned=[],
            featuresExtracted=[],
        )

        penalized = novelty.score_article_novelty(article, [same_type])
        unpenalized = novelty.score_article_novelty(article, [different_type])

        assert penalized == pytest.approx(unpenalized - novelty.ARTICLE_TYPE_WEIGHT)


class TestScoreSignalNovelty:
    """score_signal_novelty inverts the max similarity to recent signals."""

    def test_identical_signal_returns_low_novelty(self) -> None:
        """A recent signal with identical fields drives novelty to 0.0."""
        recent = _make_signal(id="sig-old")

        score = novelty.score_signal_novelty(_make_signal(), [recent])

        assert score == pytest.approx(0.0)

    def test_different_signal_type_returns_high_novelty(self) -> None:
        """A recent signal sharing nothing leaves the score fully novel."""
        recent = _make_signal(
            id="sig-old",
            signal_type="partnership_change",
            brands_mentioned=["NIO"],
            features_mentioned=["Swap 4.0"],
            source_article_ids=["art-z"],
        )

        assert novelty.score_signal_novelty(_make_signal(), [recent]) == 1.0

    def test_empty_recent_returns_1(self) -> None:
        """No recent signals means fully novel by definition."""
        assert novelty.score_signal_novelty(_make_signal(), []) == 1.0

    def test_skips_same_id(self) -> None:
        """The signal never compares against itself."""
        assert novelty.score_signal_novelty(_make_signal(), [_make_signal()]) == 1.0

    def test_same_type_same_brands_low_novelty(self) -> None:
        """Type + brand overlap alone accounts for 0.65 similarity."""
        recent = _make_signal(
            id="sig-old", features_mentioned=["City NOA"], source_article_ids=["art-x"]
        )

        score = novelty.score_signal_novelty(_make_signal(), [recent])

        assert score == pytest.approx(0.35)

    def test_same_type_different_brands_moderate_novelty(self) -> None:
        """A type match alone only accounts for 0.3 similarity."""
        recent = _make_signal(
            id="sig-old",
            brands_mentioned=["NIO"],
            features_mentioned=["Swap 4.0"],
            source_article_ids=["art-x"],
        )

        score = novelty.score_signal_novelty(_make_signal(), [recent])

        assert score == pytest.approx(0.7)

    def test_overlapping_source_articles_lowers_novelty(self) -> None:
        """A signal built from already-used source articles loses 0.15."""
        recent = _make_signal(
            id="sig-old",
            signal_type="partnership_change",
            brands_mentioned=["NIO"],
            features_mentioned=["Swap 4.0"],
        )

        score = novelty.score_signal_novelty(_make_signal(), [recent])

        assert score == pytest.approx(0.85)


@pytest.fixture
def recent_articles_mock(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Mock the Firestore recent-article fetch on the novelty module."""
    mock = AsyncMock(return_value=[])
    monkeypatch.setattr(novelty, "get_recent_processed_articles", mock)
    return mock


@pytest.fixture
def recent_signals_mock(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Mock the Firestore recent-signal fetch on the novelty module."""
    mock = AsyncMock(return_value=[])
    monkeypatch.setattr(novelty, "get_recent_signals", mock)
    return mock


class TestScoreArticleBatch:
    """score_article_batch scores inputs against the fetched recent window."""

    async def test_scores_multiple_articles(self, recent_articles_mock: AsyncMock) -> None:
        """Each input is scored; snake_case fetched docs are bridged to camelCase."""
        recent_articles_mock.return_value = [
            {
                "id": "art-old",
                "title_en": "BYD launches God's Eye ADAS on the Seal",
                "content_type": "news",
                "brands_mentioned": ["BYD"],
                "vehicles_mentioned": ["BYD Seal"],
                "features_extracted": [{"feature_name": "God's Eye", "category": "adas"}],
                "duplicate_group_id": None,
            }
        ]

        results = await novelty.score_article_batch(
            [_make_article(), _make_nio_article(id="art-b")]
        )

        assert results[0] == {"article_id": "art-a", "novelty_score": pytest.approx(0.0)}
        assert results[1]["article_id"] == "art-b"
        assert results[1]["novelty_score"] > 0.7

    async def test_fetches_recent_articles_from_firestore(
        self, recent_articles_mock: AsyncMock
    ) -> None:
        """The lookback in days is converted to the db layer's hours window."""
        await novelty.score_article_batch([_make_article()], lookback_days=7)

        recent_articles_mock.assert_awaited_once_with(hours=168)

    async def test_single_failure_returns_default_1(
        self, recent_articles_mock: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One scoring failure defaults to 1.0 without dropping the others."""
        monkeypatch.setattr(
            novelty, "score_article_novelty", MagicMock(side_effect=[RuntimeError("boom"), 0.4])
        )

        results = await novelty.score_article_batch([_make_article(), _make_article(id="art-b")])

        assert results[0] == {"article_id": "art-a", "novelty_score": 1.0}
        assert results[1] == {"article_id": "art-b", "novelty_score": 0.4}

    async def test_empty_input_returns_empty(self, recent_articles_mock: AsyncMock) -> None:
        """An empty batch returns early without touching Firestore."""
        assert await novelty.score_article_batch([]) == []
        recent_articles_mock.assert_not_awaited()


class TestScoreSignalBatch:
    """score_signal_batch scores inputs against the fetched recent window."""

    async def test_scores_multiple_signals(self, recent_signals_mock: AsyncMock) -> None:
        """Each input signal is scored against the fetched recent signals."""
        recent_signals_mock.return_value = [_make_signal(id="sig-old")]

        results = await novelty.score_signal_batch(
            [
                _make_signal(),
                _make_signal(
                    id="sig-b",
                    signal_type="partnership_change",
                    brands_mentioned=["NIO"],
                    features_mentioned=["Swap 4.0"],
                    source_article_ids=["art-z"],
                ),
            ]
        )

        assert results[0] == {"signal_id": "sig-a", "novelty_score": pytest.approx(0.0)}
        assert results[1] == {"signal_id": "sig-b", "novelty_score": 1.0}

    async def test_fetches_recent_signals_from_firestore(
        self, recent_signals_mock: AsyncMock
    ) -> None:
        """The default 14-day lookback is passed straight to the db layer."""
        await novelty.score_signal_batch([_make_signal()])

        recent_signals_mock.assert_awaited_once_with(days=14)

    async def test_single_failure_returns_default_1(
        self, recent_signals_mock: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One scoring failure defaults to 1.0 without dropping the others."""
        monkeypatch.setattr(
            novelty, "score_signal_novelty", MagicMock(side_effect=[RuntimeError("boom"), 0.6])
        )

        results = await novelty.score_signal_batch([_make_signal(), _make_signal(id="sig-b")])

        assert results[0] == {"signal_id": "sig-a", "novelty_score": 1.0}
        assert results[1] == {"signal_id": "sig-b", "novelty_score": 0.6}

    async def test_empty_input_returns_empty(self, recent_signals_mock: AsyncMock) -> None:
        """An empty batch returns early without touching Firestore."""
        assert await novelty.score_signal_batch([]) == []
        recent_signals_mock.assert_not_awaited()
