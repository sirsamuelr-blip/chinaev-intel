"""Tests for the cross-source deduplication module.

The similarity functions are pure and tested directly. The Firestore
write step (``mark_duplicates``) monkeypatches
``dedup.update_article_dedup_fields`` on the module (matching the
``test_entities`` pattern) — no real Firestore client is touched.
Article fixtures use camelCase keys, matching the Firestore doc shape
the dedup functions expect.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from processing import dedup


def _make_article(**overrides: Any) -> dict[str, Any]:
    """Build a camelCase article doc (Firestore shape) with overridable fields."""
    article: dict[str, Any] = {
        "id": "doc-a",
        "titleEn": "BYD launches God's Eye ADAS on the Seal",
        "sourceName": "gasgoo",
        "brandsMentioned": ["BYD"],
        "vehiclesMentioned": ["BYD Seal"],
        "featuresExtracted": [{"feature_name": "God's Eye", "category": "adas"}],
        "publishDate": "2026-07-17T08:00:00+00:00",
        "scrapeDate": "2026-07-17T10:00:00+00:00",
        "relevanceScore": 8,
    }
    article.update(overrides)
    return article


def _make_nio_article(**overrides: Any) -> dict[str, Any]:
    """Build an article about an unrelated NIO story (no overlap with BYD base)."""
    article = _make_article(
        titleEn="NIO opens its 3000th battery swap station nationwide",
        brandsMentioned=["NIO"],
        vehiclesMentioned=["NIO ET5"],
        featuresExtracted=[{"feature_name": "Swap 4.0", "category": "battery_software"}],
    )
    article.update(overrides)
    return article


class TestTitleSimilarity:
    """title_similarity fuzzy-matches English titles."""

    def test_identical_titles_returns_1(self) -> None:
        """The same title scores a perfect 1.0."""
        title = "BYD Launches New ADAS Feature"

        assert dedup.title_similarity(title, title) == 1.0

    def test_completely_different_titles_returns_low(self) -> None:
        """Unrelated titles score well below the useful range."""
        score = dedup.title_similarity(
            "BYD Launches New ADAS Feature",
            "Weekly steel price index update for Q3",
        )

        assert score < 0.5

    def test_similar_titles_returns_high(self) -> None:
        """Two headlines about the same story score high despite rewording."""
        score = dedup.title_similarity(
            "BYD Launches New ADAS Feature",
            "BYD Launches ADAS System",
        )

        assert score > 0.6

    def test_empty_title_returns_0(self) -> None:
        """An empty title on either side scores 0.0."""
        assert dedup.title_similarity("", "BYD Launches New ADAS Feature") == 0.0
        assert dedup.title_similarity("BYD Launches New ADAS Feature", "") == 0.0

    def test_none_title_returns_0(self) -> None:
        """A missing title on either side scores 0.0."""
        assert dedup.title_similarity(None, "BYD Launches New ADAS Feature") == 0.0
        assert dedup.title_similarity("BYD Launches New ADAS Feature", None) == 0.0


class TestJaccardSimilarity:
    """jaccard_similarity measures set overlap."""

    def test_identical_sets_returns_1(self) -> None:
        """Equal sets score a perfect 1.0."""
        assert dedup.jaccard_similarity({"BYD", "NIO"}, {"BYD", "NIO"}) == 1.0

    def test_no_overlap_returns_0(self) -> None:
        """Disjoint sets score 0.0."""
        assert dedup.jaccard_similarity({"BYD"}, {"NIO"}) == 0.0

    def test_partial_overlap(self) -> None:
        """One shared element out of three total scores 1/3."""
        score = dedup.jaccard_similarity({"BYD", "NIO"}, {"BYD", "XPENG"})

        assert score == pytest.approx(1 / 3)

    def test_both_empty_returns_0(self) -> None:
        """Two empty sets score 0.0 — shared emptiness is not a signal."""
        assert dedup.jaccard_similarity(set(), set()) == 0.0

    def test_case_insensitive(self) -> None:
        """Casing differences do not break the match."""
        assert dedup.jaccard_similarity({"BYD"}, {"byd"}) == 1.0


class TestDateWithinWindow:
    """date_within_window applies the 72-hour dedup window."""

    def test_within_window_returns_true(self) -> None:
        """Dates 24 hours apart are inside the default window."""
        date_a = datetime(2026, 7, 17, 8, 0, tzinfo=UTC)
        date_b = datetime(2026, 7, 18, 8, 0, tzinfo=UTC)

        assert dedup.date_within_window(date_a, date_b) is True

    def test_outside_window_returns_false(self) -> None:
        """Dates 97 hours apart are outside the default window."""
        date_a = datetime(2026, 7, 17, 8, 0, tzinfo=UTC)
        date_b = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)

        assert dedup.date_within_window(date_a, date_b) is False

    def test_exactly_at_boundary(self) -> None:
        """Exactly 72 hours apart counts as within the window (inclusive)."""
        date_a = datetime(2026, 7, 17, 8, 0, tzinfo=UTC)
        date_b = date_a + timedelta(hours=dedup.DEDUP_WINDOW_HOURS)

        assert dedup.date_within_window(date_a, date_b) is True

    def test_handles_string_dates(self) -> None:
        """ISO 8601 strings are parsed before comparison."""
        assert (
            dedup.date_within_window("2026-07-17T08:00:00+00:00", "2026-07-18T08:00:00+00:00")
            is True
        )
        assert (
            dedup.date_within_window("2026-07-17T08:00:00+00:00", "2026-07-25T08:00:00+00:00")
            is False
        )

    def test_handles_timezone_aware(self) -> None:
        """Aware and naive datetimes compare without raising; naive means UTC."""
        aware = datetime(2026, 7, 17, 8, 0, tzinfo=UTC)
        naive = datetime(2026, 7, 17, 9, 0)  # naive on purpose

        assert dedup.date_within_window(aware, naive) is True


class TestResolveArticleDate:
    """_resolve_article_date parses a date field and falls back to scrapeDate."""

    def test_valid_publish_date_used_directly(self) -> None:
        """A parseable publishDate is returned as-is, no fallback needed."""
        article = {
            "id": "doc-a",
            "publishDate": "2026-07-17T08:00:00+00:00",
            "scrapeDate": "2026-07-17T10:00:00+00:00",
        }

        assert dedup._resolve_article_date(article, "publishDate") == datetime(
            2026, 7, 17, 8, 0, tzinfo=UTC
        )

    def test_empty_string_falls_back_without_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An empty publishDate falls back to scrapeDate and logs nothing."""
        article = {"id": "doc-a", "publishDate": "", "scrapeDate": "2026-07-17T10:00:00+00:00"}

        with caplog.at_level(logging.DEBUG, logger="processing.dedup"):
            resolved = dedup._resolve_article_date(article, "publishDate")

        assert resolved == datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
        assert caplog.records == []

    def test_none_falls_back_without_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """A None publishDate falls back to scrapeDate and logs nothing."""
        article = {"id": "doc-a", "publishDate": None, "scrapeDate": "2026-07-17T10:00:00+00:00"}

        with caplog.at_level(logging.DEBUG, logger="processing.dedup"):
            resolved = dedup._resolve_article_date(article, "publishDate")

        assert resolved == datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
        assert caplog.records == []

    def test_missing_key_falls_back_without_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """An absent publishDate key falls back to scrapeDate and logs nothing."""
        article = {"id": "doc-a", "scrapeDate": "2026-07-17T10:00:00+00:00"}

        with caplog.at_level(logging.DEBUG, logger="processing.dedup"):
            resolved = dedup._resolve_article_date(article, "publishDate")

        assert resolved == datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
        assert caplog.records == []

    def test_malformed_string_logs_debug_and_falls_back(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A non-empty unparseable publishDate logs at DEBUG and falls back."""
        article = {
            "id": "doc-a",
            "publishDate": "not-a-real-date",
            "scrapeDate": "2026-07-17T10:00:00+00:00",
        }

        with caplog.at_level(logging.DEBUG, logger="processing.dedup"):
            resolved = dedup._resolve_article_date(article, "publishDate")

        assert resolved == datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelno == logging.DEBUG
        assert "doc-a" in record.getMessage()
        assert "not-a-real-date" in record.getMessage()

    def test_never_emits_warning_on_bad_dates(self, caplog: pytest.LogCaptureFixture) -> None:
        """Neither empty nor malformed dates ever produce a WARNING (the old spam)."""
        with caplog.at_level(logging.DEBUG, logger="processing.dedup"):
            dedup._resolve_article_date({"id": "x", "publishDate": ""}, "publishDate")
            dedup._resolve_article_date({"id": "y", "publishDate": "garbage"}, "publishDate")

        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_no_fallback_when_field_is_scrape_date(self, caplog: pytest.LogCaptureFixture) -> None:
        """A malformed scrapeDate logs at DEBUG and returns None (no self-fallback)."""
        article = {"id": "doc-a", "scrapeDate": "bogus"}

        with caplog.at_level(logging.DEBUG, logger="processing.dedup"):
            resolved = dedup._resolve_article_date(article, "scrapeDate", fallback_field=None)

        assert resolved is None
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.DEBUG


class TestExtractFeatureCategories:
    """extract_feature_categories pulls unique categories from feature arrays."""

    def test_extracts_unique_categories(self) -> None:
        """Duplicate categories collapse into a set."""
        features = [
            {"feature_name": "City NOA", "category": "adas"},
            {"feature_name": "Highway NOA", "category": "adas"},
            {"feature_name": "Voice AI", "category": "ai_assistant"},
        ]

        assert dedup.extract_feature_categories(features) == {"adas", "ai_assistant"}

    def test_empty_list_returns_empty_set(self) -> None:
        """An empty array yields an empty set."""
        assert dedup.extract_feature_categories([]) == set()

    def test_none_returns_empty_set(self) -> None:
        """A missing array yields an empty set."""
        assert dedup.extract_feature_categories(None) == set()


class TestComputeSimilarity:
    """compute_similarity combines the five weighted dimensions."""

    def test_identical_articles_returns_high(self) -> None:
        """Identical field values from two sources score a full 1.0."""
        article_a = _make_article()
        article_b = _make_article(id="doc-b", sourceName="cnevpost")

        assert dedup.compute_similarity(article_a, article_b) == pytest.approx(1.0)

    def test_different_articles_returns_low(self) -> None:
        """Unrelated stories in the same window stay far below the threshold."""
        score = dedup.compute_similarity(
            _make_article(), _make_nio_article(id="doc-b", sourceName="cnevpost")
        )

        assert score < dedup.SIMILARITY_THRESHOLD

    def test_same_brands_different_content(self) -> None:
        """Brand overlap alone (BYD price cut vs BYD ADAS) does not merge stories."""
        article_a = _make_article()
        article_b = _make_article(
            id="doc-b",
            sourceName="cnevpost",
            titleEn="BYD cuts prices across its lineup amid competition",
            vehiclesMentioned=["BYD Qin"],
            featuresExtracted=[],
        )

        assert dedup.compute_similarity(article_a, article_b) < dedup.SIMILARITY_THRESHOLD

    def test_outside_date_window_returns_0(self) -> None:
        """Articles four days apart score exactly 0.0 — no other checks run."""
        article_a = _make_article()
        article_b = _make_article(
            id="doc-b", sourceName="cnevpost", publishDate="2026-07-21T09:00:00+00:00"
        )

        assert dedup.compute_similarity(article_a, article_b) == 0.0

    def test_handles_missing_fields(self) -> None:
        """A missing array scores 0.0 on its dimension instead of raising."""
        article_a = _make_article()
        article_b = _make_article(id="doc-b", sourceName="cnevpost")
        del article_b["vehiclesMentioned"]

        assert dedup.compute_similarity(article_a, article_b) == pytest.approx(0.8)

    def test_empty_publish_date_falls_back_to_scrape_date(self) -> None:
        """Empty publishDates fall back to scrapeDate so the window still applies."""
        article_a = _make_article(publishDate="")
        article_b = _make_article(id="doc-b", sourceName="cnevpost", publishDate="")

        # Both scrapeDates default to within the window, so the pair is still
        # compared instead of being forced to 0.0 by the missing publish date.
        assert dedup.compute_similarity(article_a, article_b) == pytest.approx(1.0)


class TestFindDuplicatesForArticle:
    """find_duplicates_for_article filters and ranks candidate matches."""

    async def test_finds_duplicates_above_threshold(self) -> None:
        """A near-identical article from another source is returned with its score."""
        article = _make_article()
        candidate = _make_article(id="doc-b", sourceName="cnevpost")

        matches = await dedup.find_duplicates_for_article(article, [candidate])

        assert len(matches) == 1
        doc_id, score = matches[0]
        assert doc_id == "doc-b"
        assert score >= dedup.SIMILARITY_THRESHOLD

    async def test_excludes_same_source(self) -> None:
        """Identical content from the same source is skipped (URL dedup's job)."""
        article = _make_article()
        candidate = _make_article(id="doc-b")

        assert await dedup.find_duplicates_for_article(article, [candidate]) == []

    async def test_returns_sorted_by_score(self) -> None:
        """Matches come back ordered by score descending."""
        article = _make_article()
        partial = _make_article(
            id="doc-partial", sourceName="baidu_news", vehiclesMentioned=["BYD Han"]
        )
        exact = _make_article(id="doc-exact", sourceName="cnevpost")

        matches = await dedup.find_duplicates_for_article(article, [partial, exact])

        assert [doc_id for doc_id, _ in matches] == ["doc-exact", "doc-partial"]
        scores = [score for _, score in matches]
        assert scores == sorted(scores, reverse=True)

    async def test_no_matches_returns_empty(self) -> None:
        """An unrelated candidate yields no matches."""
        article = _make_article()
        candidate = _make_nio_article(id="doc-b", sourceName="cnevpost")

        assert await dedup.find_duplicates_for_article(article, [candidate]) == []


class TestDeduplicateArticles:
    """deduplicate_articles clusters a batch into duplicate groups."""

    async def test_finds_duplicate_group(self) -> None:
        """Two articles from different sources about one story form one group."""
        articles = [
            _make_article(),
            _make_article(id="doc-b", sourceName="cnevpost"),
        ]

        result = await dedup.deduplicate_articles(articles)

        assert result["total_compared"] == 2
        assert result["duplicate_groups_found"] == 1
        assert result["articles_marked_duplicate"] == 1
        (group,) = result["groups"]
        assert group["duplicate_group_id"]
        assert {group["canonical_id"], *group["duplicate_ids"]} == {"doc-a", "doc-b"}
        assert group["similarity_scores"] == [pytest.approx(1.0)]

    async def test_canonical_is_earlier_scrape_date(self) -> None:
        """The earliest-scraped article becomes canonical regardless of order."""
        later = _make_article(id="doc-later", scrapeDate="2026-07-17T12:00:00+00:00")
        earlier = _make_article(
            id="doc-earlier", sourceName="cnevpost", scrapeDate="2026-07-17T06:00:00+00:00"
        )

        result = await dedup.deduplicate_articles([later, earlier])

        (group,) = result["groups"]
        assert group["canonical_id"] == "doc-earlier"
        assert group["duplicate_ids"] == ["doc-later"]

    async def test_canonical_tiebreaker_is_relevance_score(self) -> None:
        """With equal scrape dates the higher relevanceScore wins."""
        low = _make_article(id="doc-low", relevanceScore=5)
        high = _make_article(id="doc-high", sourceName="cnevpost", relevanceScore=9)

        result = await dedup.deduplicate_articles([low, high])

        (group,) = result["groups"]
        assert group["canonical_id"] == "doc-high"
        assert group["duplicate_ids"] == ["doc-low"]

    async def test_no_duplicates_returns_empty_groups(self) -> None:
        """Unrelated articles produce no groups and zero counts."""
        articles = [
            _make_article(),
            _make_nio_article(id="doc-b", sourceName="cnevpost"),
        ]

        result = await dedup.deduplicate_articles(articles)

        assert result == {
            "total_compared": 2,
            "duplicate_groups_found": 0,
            "articles_marked_duplicate": 0,
            "groups": [],
        }

    async def test_three_articles_same_story(self) -> None:
        """The same story from three sources forms a single group of three."""
        articles = [
            _make_article(),
            _make_article(id="doc-b", sourceName="cnevpost"),
            _make_article(id="doc-c", sourceName="baidu_news"),
        ]

        result = await dedup.deduplicate_articles(articles)

        assert result["duplicate_groups_found"] == 1
        assert result["articles_marked_duplicate"] == 2
        (group,) = result["groups"]
        assert group["canonical_id"] == "doc-a"
        assert sorted(group["duplicate_ids"]) == ["doc-b", "doc-c"]
        assert len(group["similarity_scores"]) == 3

    async def test_independent_stories_not_merged(self) -> None:
        """Two distinct stories, each covered twice, form two separate groups."""
        articles = [
            _make_article(id="byd-1"),
            _make_article(id="byd-2", sourceName="cnevpost"),
            _make_nio_article(id="nio-1"),
            _make_nio_article(id="nio-2", sourceName="cnevpost"),
        ]

        result = await dedup.deduplicate_articles(articles)

        assert result["duplicate_groups_found"] == 2
        assert result["articles_marked_duplicate"] == 2
        grouped_ids = [
            {group["canonical_id"], *group["duplicate_ids"]} for group in result["groups"]
        ]
        assert {"byd-1", "byd-2"} in grouped_ids
        assert {"nio-1", "nio-2"} in grouped_ids


@pytest.fixture
def update_mock(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Mock the Firestore dedup-field writer on the dedup module."""
    mock = AsyncMock()
    monkeypatch.setattr(dedup, "update_article_dedup_fields", mock)
    return mock


def _make_group(**overrides: Any) -> dict[str, Any]:
    """Build a duplicate group in the deduplicate_articles output shape."""
    group: dict[str, Any] = {
        "duplicate_group_id": "group-1",
        "canonical_id": "doc-a",
        "duplicate_ids": ["doc-b", "doc-c"],
        "similarity_scores": [0.92, 0.88],
    }
    group.update(overrides)
    return group


class TestMarkDuplicates:
    """mark_duplicates writes group assignments back to Firestore."""

    async def test_marks_canonical_correctly(self, update_mock: AsyncMock) -> None:
        """The canonical gets isDuplicate=False and a null canonicalArticleId."""
        await dedup.mark_duplicates([_make_group()])

        assert update_mock.await_args_list[0].args == ("doc-a", False, None, "group-1")

    async def test_marks_duplicate_correctly(self, update_mock: AsyncMock) -> None:
        """Each duplicate gets isDuplicate=True pointing at the canonical."""
        await dedup.mark_duplicates([_make_group()])

        assert update_mock.await_args_list[1].args == ("doc-b", True, "doc-a", "group-1")
        assert update_mock.await_args_list[2].args == ("doc-c", True, "doc-a", "group-1")

    async def test_sets_duplicate_group_id_on_all(self, update_mock: AsyncMock) -> None:
        """Every member of the group shares the duplicateGroupId."""
        updated = await dedup.mark_duplicates([_make_group()])

        assert updated == 3
        assert [call.args[3] for call in update_mock.await_args_list] == ["group-1"] * 3

    async def test_handles_empty_groups(self, update_mock: AsyncMock) -> None:
        """No groups means no writes and a zero count."""
        assert await dedup.mark_duplicates([]) == 0
        update_mock.assert_not_awaited()
