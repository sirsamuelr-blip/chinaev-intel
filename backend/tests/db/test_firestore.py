"""Tests for db.firestore helpers.

All Firestore operations are mocked — no real client, emulator, or
network. The mock client mirrors Firestore's chained API:
``collection() -> where()/order_by()/limit() -> get()`` and
``collection() -> document() -> update()``, with async leaf calls
(``add``, ``get``, ``update``) as AsyncMock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.cloud.firestore import SERVER_TIMESTAMP

from db import firestore

DOC_ID = "doc-abc-123"

SAMPLE_ARTICLE = {
    "source_name": "gasgoo",
    "source_url": "https://autonews.gasgoo.com/article-1",
    "title_zh": "小鹏发布新智驾系统",
    "title_en": "XPeng launches new ADAS",
    "body_zh": "正文",
    "body_en": "Body text",
    "publish_date": "2026-07-17T08:00:00+00:00",
    "scrape_date": "2026-07-18T02:00:00+00:00",
}

SAMPLE_EXTRACTED = {
    "title_en": "XPeng launches new ADAS",
    "body_en": "Full translation",
    "relevance_score": 9,
    "content_type": "news",
    "brands_mentioned": ["XPeng"],
    "vehicles_mentioned": ["XPeng P7"],
    "features_extracted": [{"feature_name": "City NOA", "category": "adas"}],
    "competitive_signal": "City NOA rollout accelerating.",
}

SAMPLE_METRICS = {
    "source_name": "gasgoo",
    "status": "success",
    "articles_ingested": 12,
    "requests_made": 15,
    "error_count": 0,
    "errors": [],
    "duration_seconds": 143.2,
}


def _make_doc_ref(doc_id: str = DOC_ID) -> MagicMock:
    """Build a mock document reference with an async ``update``."""
    doc_ref = MagicMock()
    doc_ref.id = doc_id
    doc_ref.update = AsyncMock()
    return doc_ref


def _make_snapshot(doc_id: str, data: dict[str, Any]) -> MagicMock:
    """Build a mock document snapshot returning ``data`` from ``to_dict``."""
    snapshot = MagicMock()
    snapshot.id = doc_id
    snapshot.to_dict.return_value = data
    return snapshot


def _make_query(results: list[MagicMock]) -> MagicMock:
    """Build a mock query whose chained calls return itself and yield ``results``."""
    query = MagicMock()
    query.where.return_value = query
    query.order_by.return_value = query
    query.limit.return_value = query
    query.get = AsyncMock(return_value=results)
    return query


@pytest.fixture
def doc_ref() -> MagicMock:
    """Mock document reference shared by the client fixture."""
    return _make_doc_ref()


@pytest.fixture
def collection_ref(doc_ref: MagicMock) -> MagicMock:
    """Mock collection reference with async ``add`` and chainable ``where``."""
    collection = MagicMock()
    collection.add = AsyncMock(return_value=(MagicMock(), doc_ref))
    collection.document.return_value = doc_ref
    collection.where.return_value = _make_query([])
    return collection


@pytest.fixture
def mock_db(monkeypatch: pytest.MonkeyPatch, collection_ref: MagicMock) -> MagicMock:
    """Mock AsyncClient patched into the module-level ``_db`` cache."""
    db = MagicMock()
    db.collection.return_value = collection_ref
    monkeypatch.setattr(firestore, "_db", db)
    return db


class TestSaveArticle:
    """save_article writes camelCased docs to the articles collection."""

    async def test_save_article_writes_to_collection(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """The doc is camelCased with processed=False and processingError=None."""
        await firestore.save_article(SAMPLE_ARTICLE)

        mock_db.collection.assert_called_once_with("articles")
        collection_ref.add.assert_awaited_once()
        (data,) = collection_ref.add.await_args.args
        assert data["sourceName"] == "gasgoo"
        assert data["sourceUrl"] == "https://autonews.gasgoo.com/article-1"
        assert data["titleZh"] == "小鹏发布新智驾系统"
        assert data["publishDate"] == "2026-07-17T08:00:00+00:00"
        assert data["scrapeDate"] == "2026-07-18T02:00:00+00:00"
        assert data["processed"] is False
        assert data["processingError"] is None
        assert "source_name" not in data

    async def test_save_article_returns_doc_id(self, mock_db: MagicMock) -> None:
        """The returned string is the mock doc ref's auto-generated ID."""
        result = await firestore.save_article(SAMPLE_ARTICLE)

        assert result == DOC_ID


class TestArticleExists:
    """article_exists deduplicates by sourceUrl."""

    async def test_article_exists_returns_true(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """A non-empty query result means the article is already stored."""
        collection_ref.where.return_value = _make_query(
            [_make_snapshot(DOC_ID, {"sourceUrl": "https://example.com/a"})]
        )

        assert await firestore.article_exists("https://example.com/a") is True

        field_filter = collection_ref.where.call_args.kwargs["filter"]
        assert field_filter.field_path == "sourceUrl"
        assert field_filter.op_string == "=="
        assert field_filter.value == "https://example.com/a"

    async def test_article_exists_returns_false(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """An empty query result means the article is new."""
        collection_ref.where.return_value = _make_query([])

        assert await firestore.article_exists("https://example.com/new") is False


class TestGetUnprocessedArticles:
    """get_unprocessed_articles feeds the LLM pipeline queue."""

    async def test_get_unprocessed_articles_queries_correctly(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """The query filters processed==False, orders by scrapeDate, limits."""
        query = _make_query([])
        collection_ref.where.return_value = query

        await firestore.get_unprocessed_articles(limit=25)

        mock_db.collection.assert_called_once_with("articles")
        field_filter = collection_ref.where.call_args.kwargs["filter"]
        assert field_filter.field_path == "processed"
        assert field_filter.op_string == "=="
        assert field_filter.value is False
        query.order_by.assert_called_once_with("scrapeDate")
        query.limit.assert_called_once_with(25)

    async def test_get_unprocessed_articles_returns_dicts_with_ids(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """Each result carries the doc ID and snake_case field names."""
        collection_ref.where.return_value = _make_query(
            [
                _make_snapshot("doc-1", {"sourceName": "gasgoo", "titleEn": "One"}),
                _make_snapshot("doc-2", {"sourceName": "cnevpost", "titleEn": "Two"}),
            ]
        )

        articles = await firestore.get_unprocessed_articles()

        assert articles == [
            {"id": "doc-1", "source_name": "gasgoo", "title_en": "One"},
            {"id": "doc-2", "source_name": "cnevpost", "title_en": "Two"},
        ]

    async def test_get_unprocessed_articles_empty(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """An empty query result returns an empty list."""
        collection_ref.where.return_value = _make_query([])

        assert await firestore.get_unprocessed_articles() == []


class TestUpdateArticleAfterProcessing:
    """update_article_after_processing writes extraction results back."""

    async def test_update_article_after_processing_sets_fields(
        self, mock_db: MagicMock, collection_ref: MagicMock, doc_ref: MagicMock
    ) -> None:
        """Extracted fields are camelCased; processed=True, error cleared."""
        await firestore.update_article_after_processing(DOC_ID, SAMPLE_EXTRACTED)

        collection_ref.document.assert_called_once_with(DOC_ID)
        doc_ref.update.assert_awaited_once()
        (updates,) = doc_ref.update.await_args.args
        assert updates["titleEn"] == "XPeng launches new ADAS"
        assert updates["bodyEn"] == "Full translation"
        assert updates["relevanceScore"] == 9
        assert updates["contentType"] == "news"
        assert updates["brandsMentioned"] == ["XPeng"]
        assert updates["vehiclesMentioned"] == ["XPeng P7"]
        assert updates["featuresExtracted"] == [{"feature_name": "City NOA", "category": "adas"}]
        assert updates["competitiveSignal"] == "City NOA rollout accelerating."
        assert updates["processed"] is True
        assert updates["processingError"] is None


class TestSetArticleProcessingError:
    """set_article_processing_error keeps failed articles in the queue."""

    async def test_set_article_processing_error_sets_error(
        self, mock_db: MagicMock, collection_ref: MagicMock, doc_ref: MagicMock
    ) -> None:
        """processingError is set and processed is left untouched."""
        await firestore.set_article_processing_error(DOC_ID, "malformed JSON")

        collection_ref.document.assert_called_once_with(DOC_ID)
        doc_ref.update.assert_awaited_once()
        (updates,) = doc_ref.update.await_args.args
        assert updates["processingError"] == "malformed JSON"
        assert "processed" not in updates


class TestGetRecentProcessedArticles:
    """get_recent_processed_articles feeds the dedup comparison window."""

    async def test_get_recent_processed_articles_returns_list(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """Processed articles inside the window are returned; stale ones drop."""
        recent = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        stale = (datetime.now(UTC) - timedelta(hours=100)).isoformat()
        query = _make_query(
            [
                _make_snapshot("doc-1", {"sourceName": "gasgoo", "scrapeDate": recent}),
                _make_snapshot("doc-2", {"sourceName": "cnevpost", "scrapeDate": stale}),
            ]
        )
        collection_ref.where.return_value = query

        articles = await firestore.get_recent_processed_articles(hours=72)

        mock_db.collection.assert_called_once_with("articles")
        field_filter = collection_ref.where.call_args.kwargs["filter"]
        assert field_filter.field_path == "processed"
        assert field_filter.op_string == "=="
        assert field_filter.value is True
        query.order_by.assert_called_once_with("scrapeDate", direction="DESCENDING")
        assert articles == [{"id": "doc-1", "source_name": "gasgoo", "scrape_date": recent}]

    async def test_get_recent_processed_articles_empty(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """An empty query result returns an empty list."""
        collection_ref.where.return_value = _make_query([])

        assert await firestore.get_recent_processed_articles() == []


class TestUpdateArticleDedupFields:
    """update_article_dedup_fields writes ADR 004 dedup fields."""

    async def test_update_article_dedup_fields_sets_all_fields(
        self, mock_db: MagicMock, collection_ref: MagicMock, doc_ref: MagicMock
    ) -> None:
        """All three camelCase dedup fields land in a single update."""
        await firestore.update_article_dedup_fields(
            DOC_ID,
            is_duplicate=True,
            canonical_article_id="doc-canonical",
            duplicate_group_id="group-1",
        )

        collection_ref.document.assert_called_once_with(DOC_ID)
        doc_ref.update.assert_awaited_once()
        (updates,) = doc_ref.update.await_args.args
        assert updates == {
            "isDuplicate": True,
            "canonicalArticleId": "doc-canonical",
            "duplicateGroupId": "group-1",
        }


class TestGetArticlesByDuplicateGroup:
    """get_articles_by_duplicate_group returns a duplicate cluster."""

    async def test_get_articles_by_duplicate_group_found(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """Matching docs come back with snake_case keys and their IDs."""
        collection_ref.where.return_value = _make_query(
            [_make_snapshot("doc-1", {"duplicateGroupId": "group-1", "isDuplicate": True})]
        )

        result = await firestore.get_articles_by_duplicate_group("group-1")

        assert result == [{"id": "doc-1", "duplicate_group_id": "group-1", "is_duplicate": True}]
        field_filter = collection_ref.where.call_args.kwargs["filter"]
        assert field_filter.field_path == "duplicateGroupId"
        assert field_filter.op_string == "=="
        assert field_filter.value == "group-1"

    async def test_get_articles_by_duplicate_group_not_found(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """No matching group returns an empty list."""
        collection_ref.where.return_value = _make_query([])

        assert await firestore.get_articles_by_duplicate_group("missing-group") == []


class TestSaveHealthMetrics:
    """save_health_metrics writes run stats to scraper_health."""

    async def test_save_health_metrics_writes_to_collection(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """The doc is camelCased and runTimestamp uses SERVER_TIMESTAMP."""
        await firestore.save_health_metrics(SAMPLE_METRICS)

        mock_db.collection.assert_called_once_with("scraper_health")
        collection_ref.add.assert_awaited_once()
        (data,) = collection_ref.add.await_args.args
        assert data["sourceName"] == "gasgoo"
        assert data["status"] == "success"
        assert data["articlesIngested"] == 12
        assert data["requestsMade"] == 15
        assert data["errorCount"] == 0
        assert data["errors"] == []
        assert data["durationSeconds"] == 143.2
        assert data["runTimestamp"] is SERVER_TIMESTAMP

    async def test_save_health_metrics_returns_doc_id(self, mock_db: MagicMock) -> None:
        """The returned string is the mock doc ref's auto-generated ID."""
        result = await firestore.save_health_metrics(SAMPLE_METRICS)

        assert result == DOC_ID


SAMPLE_BRAND = {
    "name_en": "BYD",
    "name_zh": "比亚迪",
    "parent_group": "BYD",
    "ev_focus": False,
}

SAMPLE_VEHICLE = {
    "brand_id": "brand-1",
    "brand_name_en": "BYD",
    "model_name_en": "Seal",
}

SAMPLE_FEATURE = {
    "brand_id": "brand-1",
    "brand_name_en": "BYD",
    "feature_name_en": "City NOA",
    "category": "adas",
    "description": "Urban navigate-on-autopilot",
    "supplier": None,
    "launch_type": "new",
}


class TestGetBrandByNameEn:
    """get_brand_by_name_en looks up brands by canonical English name."""

    async def test_get_brand_by_name_en_found(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """A matching doc is returned with snake_case keys and its ID."""
        collection_ref.where.return_value = _make_query(
            [_make_snapshot("brand-1", {"nameEn": "BYD", "nameZh": "比亚迪"})]
        )

        result = await firestore.get_brand_by_name_en("BYD")

        assert result == {"id": "brand-1", "name_en": "BYD", "name_zh": "比亚迪"}
        mock_db.collection.assert_called_once_with("brands")
        field_filter = collection_ref.where.call_args.kwargs["filter"]
        assert field_filter.field_path == "nameEn"
        assert field_filter.op_string == "=="
        assert field_filter.value == "BYD"

    async def test_get_brand_by_name_en_not_found(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """No match returns None."""
        collection_ref.where.return_value = _make_query([])

        assert await firestore.get_brand_by_name_en("Tesla") is None


class TestUpsertBrand:
    """upsert_brand creates or updates brand docs keyed by nameEn."""

    async def test_upsert_brand_creates_new(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """With no existing doc the brand is added with camelCase fields."""
        collection_ref.where.return_value = _make_query([])

        result = await firestore.upsert_brand(SAMPLE_BRAND)

        assert result == DOC_ID
        collection_ref.add.assert_awaited_once()
        (data,) = collection_ref.add.await_args.args
        assert data["nameEn"] == "BYD"
        assert data["nameZh"] == "比亚迪"
        assert data["parentGroup"] == "BYD"
        assert data["evFocus"] is False
        assert data["lastUpdated"] is SERVER_TIMESTAMP
        assert "name_en" not in data

    async def test_upsert_brand_updates_existing(
        self, mock_db: MagicMock, collection_ref: MagicMock, doc_ref: MagicMock
    ) -> None:
        """With an existing doc the brand is updated in place."""
        collection_ref.where.return_value = _make_query(
            [_make_snapshot("brand-1", {"nameEn": "BYD"})]
        )

        result = await firestore.upsert_brand(SAMPLE_BRAND)

        assert result == "brand-1"
        collection_ref.add.assert_not_awaited()
        collection_ref.document.assert_called_once_with("brand-1")
        doc_ref.update.assert_awaited_once()
        (updates,) = doc_ref.update.await_args.args
        assert updates["nameEn"] == "BYD"
        assert updates["lastUpdated"] is SERVER_TIMESTAMP


class TestGetVehicleByModel:
    """get_vehicle_by_model looks up vehicles by brand ID and model name."""

    async def test_get_vehicle_by_model_found(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """A matching doc is returned; both equality filters are applied."""
        query = _make_query(
            [_make_snapshot("veh-1", {"brandId": "brand-1", "modelNameEn": "Seal"})]
        )
        collection_ref.where.return_value = query

        result = await firestore.get_vehicle_by_model("brand-1", "Seal")

        assert result == {"id": "veh-1", "brand_id": "brand-1", "model_name_en": "Seal"}
        mock_db.collection.assert_called_once_with("vehicles")
        first_filter = collection_ref.where.call_args.kwargs["filter"]
        assert first_filter.field_path == "brandId"
        assert first_filter.value == "brand-1"
        second_filter = query.where.call_args.kwargs["filter"]
        assert second_filter.field_path == "modelNameEn"
        assert second_filter.value == "Seal"

    async def test_get_vehicle_by_model_not_found(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """No match returns None."""
        collection_ref.where.return_value = _make_query([])

        assert await firestore.get_vehicle_by_model("brand-1", "Nonexistent") is None


class TestUpsertVehicle:
    """upsert_vehicle creates or updates vehicles keyed by brand + model."""

    async def test_upsert_vehicle_creates_new(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """With no existing doc the vehicle is added with camelCase fields."""
        collection_ref.where.return_value = _make_query([])

        result = await firestore.upsert_vehicle(SAMPLE_VEHICLE)

        assert result == DOC_ID
        collection_ref.add.assert_awaited_once()
        (data,) = collection_ref.add.await_args.args
        assert data["brandId"] == "brand-1"
        assert data["brandNameEn"] == "BYD"
        assert data["modelNameEn"] == "Seal"
        assert data["lastUpdated"] is SERVER_TIMESTAMP

    async def test_upsert_vehicle_updates_existing(
        self, mock_db: MagicMock, collection_ref: MagicMock, doc_ref: MagicMock
    ) -> None:
        """With an existing doc the vehicle is updated in place."""
        collection_ref.where.return_value = _make_query(
            [_make_snapshot("veh-1", {"brandId": "brand-1", "modelNameEn": "Seal"})]
        )

        result = await firestore.upsert_vehicle(SAMPLE_VEHICLE)

        assert result == "veh-1"
        collection_ref.add.assert_not_awaited()
        collection_ref.document.assert_called_once_with("veh-1")
        doc_ref.update.assert_awaited_once()


class TestGetFeature:
    """get_feature looks up features by brand ID, feature name, and category."""

    async def test_get_feature_found(self, mock_db: MagicMock, collection_ref: MagicMock) -> None:
        """A matching doc is returned; all three equality filters are applied."""
        query = _make_query(
            [_make_snapshot("feat-1", {"brandId": "brand-1", "featureNameEn": "City NOA"})]
        )
        collection_ref.where.return_value = query

        result = await firestore.get_feature("brand-1", "City NOA", "adas")

        assert result == {"id": "feat-1", "brand_id": "brand-1", "feature_name_en": "City NOA"}
        mock_db.collection.assert_called_once_with("features")
        first_filter = collection_ref.where.call_args.kwargs["filter"]
        assert first_filter.field_path == "brandId"
        chained_paths = [call.kwargs["filter"].field_path for call in query.where.call_args_list]
        assert chained_paths == ["featureNameEn", "category"]

    async def test_get_feature_not_found(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """No match returns None."""
        collection_ref.where.return_value = _make_query([])

        assert await firestore.get_feature("brand-1", "Unknown", "adas") is None


class TestUpsertFeature:
    """upsert_feature creates or updates features, protecting firstSeenDate."""

    async def test_upsert_feature_creates_new_with_first_seen_date(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """A new feature doc gets firstSeenDate set to the server timestamp."""
        collection_ref.where.return_value = _make_query([])

        result = await firestore.upsert_feature(SAMPLE_FEATURE)

        assert result == DOC_ID
        collection_ref.add.assert_awaited_once()
        (data,) = collection_ref.add.await_args.args
        assert data["brandId"] == "brand-1"
        assert data["featureNameEn"] == "City NOA"
        assert data["category"] == "adas"
        assert data["launchType"] == "new"
        assert data["firstSeenDate"] is SERVER_TIMESTAMP
        assert data["lastUpdated"] is SERVER_TIMESTAMP

    async def test_upsert_feature_updates_without_overwriting_first_seen_date(
        self, mock_db: MagicMock, collection_ref: MagicMock, doc_ref: MagicMock
    ) -> None:
        """An existing feature doc is updated without touching firstSeenDate."""
        collection_ref.where.return_value = _make_query(
            [_make_snapshot("feat-1", {"featureNameEn": "City NOA"})]
        )

        result = await firestore.upsert_feature(SAMPLE_FEATURE)

        assert result == "feat-1"
        collection_ref.add.assert_not_awaited()
        doc_ref.update.assert_awaited_once()
        (updates,) = doc_ref.update.await_args.args
        assert "firstSeenDate" not in updates
        assert updates["lastUpdated"] is SERVER_TIMESTAMP


SAMPLE_SIGNAL = {
    "signal_type": "new_feature_launch",
    "title": "BYD brings city NOA to the Seal",
    "summary": "BYD is rolling urban NOA out to its volume sedan.",
    "brands_mentioned": ["BYD"],
    "features_mentioned": ["City NOA"],
    "source_article_ids": ["doc-1"],
    "implications_for_western_oems": "ADAS is reaching mass-market price points.",
    "competitive_impact_score": 7,
}


class TestSaveSignal:
    """save_signal writes camelCased docs to the signals collection."""

    async def test_save_signal_creates_doc(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """The doc lands in signals with createdDate and a pending status."""
        await firestore.save_signal(SAMPLE_SIGNAL)

        mock_db.collection.assert_called_once_with("signals")
        collection_ref.add.assert_awaited_once()
        (data,) = collection_ref.add.await_args.args
        assert data["createdDate"] is SERVER_TIMESTAMP
        assert data["status"] == "pending"

    async def test_save_signal_returns_doc_id(self, mock_db: MagicMock) -> None:
        """The returned string is the mock doc ref's auto-generated ID."""
        result = await firestore.save_signal(SAMPLE_SIGNAL)

        assert result == DOC_ID

    async def test_save_signal_applies_camel_case(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """Snake_case input fields are stored under camelCase names."""
        await firestore.save_signal(SAMPLE_SIGNAL)

        (data,) = collection_ref.add.await_args.args
        assert data["signalType"] == "new_feature_launch"
        assert data["brandsMentioned"] == ["BYD"]
        assert data["featuresMentioned"] == ["City NOA"]
        assert data["sourceArticleIds"] == ["doc-1"]
        assert data["implicationsForWesternOems"] == "ADAS is reaching mass-market price points."
        assert data["competitiveImpactScore"] == 7
        assert "signal_type" not in data


class TestGetSignalsByArticleId:
    """get_signals_by_article_id queries via array_contains on sourceArticleIds."""

    async def test_get_signals_found(self, mock_db: MagicMock, collection_ref: MagicMock) -> None:
        """Matching docs come back with snake_case keys and their IDs."""
        collection_ref.where.return_value = _make_query(
            [
                _make_snapshot(
                    "sig-1", {"signalType": "ai_integration", "sourceArticleIds": ["doc-1"]}
                )
            ]
        )

        result = await firestore.get_signals_by_article_id("doc-1")

        assert result == [
            {"id": "sig-1", "signal_type": "ai_integration", "source_article_ids": ["doc-1"]}
        ]
        mock_db.collection.assert_called_once_with("signals")
        field_filter = collection_ref.where.call_args.kwargs["filter"]
        assert field_filter.field_path == "sourceArticleIds"
        assert field_filter.op_string == "array_contains"
        assert field_filter.value == "doc-1"

    async def test_get_signals_not_found(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """No matching signals returns an empty list."""
        collection_ref.where.return_value = _make_query([])

        assert await firestore.get_signals_by_article_id("doc-none") == []


class TestGetRecentSignals:
    """get_recent_signals feeds the novelty scoring comparison window."""

    async def test_get_recent_signals_returns_list(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """Signals inside the window are returned; stale ones drop."""
        recent = datetime.now(UTC) - timedelta(days=2)
        stale = datetime.now(UTC) - timedelta(days=30)
        query = _make_query(
            [
                _make_snapshot("sig-1", {"signalType": "ai_integration", "createdDate": recent}),
                _make_snapshot("sig-2", {"signalType": "ota_deployment", "createdDate": stale}),
            ]
        )
        collection_ref.order_by.return_value = query

        result = await firestore.get_recent_signals(days=14)

        mock_db.collection.assert_called_once_with("signals")
        collection_ref.order_by.assert_called_once_with("createdDate", direction="DESCENDING")
        assert result == [{"id": "sig-1", "signal_type": "ai_integration", "created_date": recent}]

    async def test_get_recent_signals_empty(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """An empty query result returns an empty list."""
        collection_ref.order_by.return_value = _make_query([])

        assert await firestore.get_recent_signals() == []


class TestGetAllFeatures:
    """get_all_features feeds the signal detection trickle-down check."""

    async def test_get_all_features_no_filter(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """Without a category the whole collection is fetched, unfiltered."""
        collection_ref.get = AsyncMock(
            return_value=[_make_snapshot("feat-1", {"brandNameEn": "XPENG", "category": "adas"})]
        )

        result = await firestore.get_all_features()

        assert result == [{"id": "feat-1", "brand_name_en": "XPENG", "category": "adas"}]
        mock_db.collection.assert_called_once_with("features")
        collection_ref.where.assert_not_called()

    async def test_get_all_features_with_category_filter(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """With a category an equality filter is applied."""
        collection_ref.where.return_value = _make_query(
            [_make_snapshot("feat-1", {"brandNameEn": "XPENG", "category": "adas"})]
        )

        result = await firestore.get_all_features(category="adas")

        assert result == [{"id": "feat-1", "brand_name_en": "XPENG", "category": "adas"}]
        field_filter = collection_ref.where.call_args.kwargs["filter"]
        assert field_filter.field_path == "category"
        assert field_filter.op_string == "=="
        assert field_filter.value == "adas"

    async def test_get_all_features_empty(
        self, mock_db: MagicMock, collection_ref: MagicMock
    ) -> None:
        """An empty collection returns an empty list."""
        collection_ref.get = AsyncMock(return_value=[])

        assert await firestore.get_all_features() == []


class TestCaseConversion:
    """The private case-conversion helpers map field names both ways."""

    @pytest.mark.parametrize(
        ("snake", "camel"),
        [
            ("source_name", "sourceName"),
            ("source_url", "sourceUrl"),
            ("title_zh", "titleZh"),
            ("body_en", "bodyEn"),
            ("relevance_score", "relevanceScore"),
            ("features_extracted", "featuresExtracted"),
            ("duration_seconds", "durationSeconds"),
            ("processed", "processed"),
        ],
    )
    def test_snake_to_camel_conversion(self, snake: str, camel: str) -> None:
        """snake_case field names convert to the schema's camelCase names."""
        assert firestore._snake_to_camel(snake) == camel

    @pytest.mark.parametrize(
        ("camel", "snake"),
        [
            ("sourceName", "source_name"),
            ("sourceUrl", "source_url"),
            ("titleZh", "title_zh"),
            ("bodyEn", "body_en"),
            ("relevanceScore", "relevance_score"),
            ("featuresExtracted", "features_extracted"),
            ("durationSeconds", "duration_seconds"),
            ("processed", "processed"),
        ],
    )
    def test_camel_to_snake_conversion(self, camel: str, snake: str) -> None:
        """camelCase document fields convert back to snake_case."""
        assert firestore._camel_to_snake(camel) == snake
