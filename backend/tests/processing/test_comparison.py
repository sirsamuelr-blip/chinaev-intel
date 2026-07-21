"""Tests for processing.comparison analysis functions.

All Firestore reads are mocked at the module boundary: the db-layer
functions imported into ``processing.comparison`` are replaced with
AsyncMocks, so no real client, emulator, or network is involved.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from processing import comparison


def _make_feature(**overrides: Any) -> dict[str, Any]:
    """Build a feature dict shaped like a db-layer read, with overrides."""
    feature: dict[str, Any] = {
        "id": "feat-1",
        "brand_id": "brand-1",
        "brand_name_en": "BYD",
        "feature_name_en": "City NOA",
        "category": "adas",
        "description": "Urban navigate-on-autopilot",
        "supplier": None,
        "launch_type": "new",
        "first_seen_date": "2026-03-01T00:00:00+00:00",
    }
    feature.update(overrides)
    return feature


def _make_vehicle(**overrides: Any) -> dict[str, Any]:
    """Build a vehicle dict shaped like a db-layer read, with overrides."""
    vehicle: dict[str, Any] = {
        "id": "veh-1",
        "brand_id": "brand-1",
        "brand_name_en": "BYD",
        "model_name_en": "Seal",
        "segment": "sedan",
        "powertrain": "BEV",
        "price_range_cny": "150,000-200,000",
    }
    vehicle.update(overrides)
    return vehicle


def _make_brand(**overrides: Any) -> dict[str, Any]:
    """Build a brand dict shaped like a db-layer read, with overrides."""
    brand: dict[str, Any] = {
        "id": "brand-1",
        "name_en": "BYD",
        "name_zh": "比亚迪",
        "parent_group": "BYD",
        "ev_focus": False,
    }
    brand.update(overrides)
    return brand


@pytest.fixture
def db_mocks(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Replace the db-layer reads imported into comparison with AsyncMocks."""
    mocks = {
        "get_all_features": AsyncMock(return_value=[]),
        "get_all_vehicles": AsyncMock(return_value=[]),
        "get_brand_by_name_en": AsyncMock(return_value=None),
        "get_features_by_brand": AsyncMock(return_value=[]),
        "get_vehicles_by_brand": AsyncMock(return_value=[]),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(comparison, name, mock)
    return mocks


class TestGetFeatureMatrix:
    """get_feature_matrix groups features by brand and category."""

    async def test_returns_matrix_grouped_by_brand_and_category(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """Features land under their brand and category with projected fields."""
        db_mocks["get_all_features"].return_value = [
            _make_feature(id="feat-1", brand_name_en="BYD", category="adas"),
            _make_feature(
                id="feat-2",
                brand_name_en="BYD",
                category="ota",
                feature_name_en="Whole-vehicle OTA",
                supplier="BYD Semiconductor",
            ),
            _make_feature(
                id="feat-3",
                brand_name_en="NIO",
                category="ai_assistant",
                feature_name_en="NOMI GPT",
                first_seen_date=None,
            ),
        ]

        result = await comparison.get_feature_matrix()

        assert result["matrix"] == {
            "BYD": {
                "adas": [
                    {
                        "feature_name_en": "City NOA",
                        "description": "Urban navigate-on-autopilot",
                        "first_seen_date": "2026-03-01T00:00:00+00:00",
                        "supplier": None,
                    }
                ],
                "ota": [
                    {
                        "feature_name_en": "Whole-vehicle OTA",
                        "description": "Urban navigate-on-autopilot",
                        "first_seen_date": "2026-03-01T00:00:00+00:00",
                        "supplier": "BYD Semiconductor",
                    }
                ],
            },
            "NIO": {
                "ai_assistant": [
                    {
                        "feature_name_en": "NOMI GPT",
                        "description": "Urban navigate-on-autopilot",
                        "first_seen_date": None,
                        "supplier": None,
                    }
                ],
            },
        }
        assert result["total_features"] == 3
        assert result["total_brands"] == 2

    async def test_filters_by_category(self, db_mocks: dict[str, AsyncMock]) -> None:
        """The category filter is pushed down to the Firestore query."""
        db_mocks["get_all_features"].return_value = [
            _make_feature(brand_name_en="XPENG", category="adas")
        ]

        result = await comparison.get_feature_matrix(category="adas")

        db_mocks["get_all_features"].assert_awaited_once_with("adas")
        assert result["categories"] == ["adas"]
        assert result["brands"] == ["XPENG"]

    async def test_empty_features_returns_empty_matrix(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """An empty features collection yields an empty matrix with zero counts."""
        result = await comparison.get_feature_matrix()

        assert result == {
            "matrix": {},
            "brands": [],
            "categories": [],
            "total_features": 0,
            "total_brands": 0,
        }

    async def test_brands_sorted_alphabetically(self, db_mocks: dict[str, AsyncMock]) -> None:
        """The brands list is sorted regardless of fetch order."""
        db_mocks["get_all_features"].return_value = [
            _make_feature(id="feat-1", brand_name_en="XPENG"),
            _make_feature(id="feat-2", brand_name_en="BYD"),
            _make_feature(id="feat-3", brand_name_en="NIO"),
        ]

        result = await comparison.get_feature_matrix()

        assert result["brands"] == ["BYD", "NIO", "XPENG"]

    async def test_categories_only_include_populated(self, db_mocks: dict[str, AsyncMock]) -> None:
        """Only categories that actually have features appear, sorted."""
        db_mocks["get_all_features"].return_value = [
            _make_feature(id="feat-1", brand_name_en="BYD", category="ota"),
            _make_feature(id="feat-2", brand_name_en="NIO", category="adas"),
        ]

        result = await comparison.get_feature_matrix()

        assert result["categories"] == ["adas", "ota"]


class TestGetFeatureTimeline:
    """get_feature_timeline orders features by first_seen_date."""

    async def test_returns_sorted_by_first_seen_date(self, db_mocks: dict[str, AsyncMock]) -> None:
        """Features come back oldest first regardless of fetch order."""
        db_mocks["get_all_features"].return_value = [
            _make_feature(
                id="feat-1",
                feature_name_en="Later",
                first_seen_date="2026-05-01T00:00:00+00:00",
            ),
            _make_feature(
                id="feat-2",
                feature_name_en="Earliest",
                first_seen_date="2026-01-15T00:00:00+00:00",
            ),
            _make_feature(
                id="feat-3",
                feature_name_en="Middle",
                first_seen_date="2026-03-01T00:00:00+00:00",
            ),
        ]

        result = await comparison.get_feature_timeline()

        names = [entry["feature_name_en"] for entry in result["timeline"]]
        assert names == ["Earliest", "Middle", "Later"]
        assert result["total_features"] == 3
        assert result["filters_applied"] == {"category": None, "brand": None}

    async def test_filters_by_category(self, db_mocks: dict[str, AsyncMock]) -> None:
        """The category filter is pushed down and echoed in filters_applied."""
        db_mocks["get_all_features"].return_value = [_make_feature(category="ota")]

        result = await comparison.get_feature_timeline(category="ota")

        db_mocks["get_all_features"].assert_awaited_once_with("ota")
        assert result["filters_applied"] == {"category": "ota", "brand": None}
        assert result["total_features"] == 1

    async def test_filters_by_brand(self, db_mocks: dict[str, AsyncMock]) -> None:
        """The brand filter drops other brands' features in Python."""
        db_mocks["get_all_features"].return_value = [
            _make_feature(id="feat-1", brand_name_en="BYD"),
            _make_feature(id="feat-2", brand_name_en="NIO", feature_name_en="NOMI GPT"),
        ]

        result = await comparison.get_feature_timeline(brand="NIO")

        assert [entry["feature_name_en"] for entry in result["timeline"]] == ["NOMI GPT"]
        assert result["filters_applied"] == {"category": None, "brand": "NIO"}
        assert result["total_features"] == 1

    async def test_features_without_date_sort_last(self, db_mocks: dict[str, AsyncMock]) -> None:
        """Undated features trail every dated feature."""
        db_mocks["get_all_features"].return_value = [
            _make_feature(id="feat-1", feature_name_en="Undated", first_seen_date=None),
            _make_feature(
                id="feat-2",
                feature_name_en="Dated",
                first_seen_date="2026-04-01T00:00:00+00:00",
            ),
        ]

        result = await comparison.get_feature_timeline()

        names = [entry["feature_name_en"] for entry in result["timeline"]]
        assert names == ["Dated", "Undated"]
        assert result["timeline"][1]["first_seen_date"] is None

    async def test_empty_features_returns_empty_timeline(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """An empty features collection yields an empty timeline."""
        result = await comparison.get_feature_timeline()

        assert result == {
            "timeline": [],
            "filters_applied": {"category": None, "brand": None},
            "total_features": 0,
        }


class TestGetBrandComparison:
    """get_brand_comparison builds side-by-side brand profiles."""

    async def test_compares_two_brands(self, db_mocks: dict[str, AsyncMock]) -> None:
        """Both brands come back with info, vehicles, and grouped features."""
        db_mocks["get_brand_by_name_en"].side_effect = [
            _make_brand(),
            _make_brand(
                id="brand-2", name_en="NIO", name_zh="蔚来", parent_group="NIO", ev_focus=True
            ),
        ]
        db_mocks["get_features_by_brand"].side_effect = [
            [_make_feature()],
            [
                _make_feature(
                    id="feat-2",
                    brand_name_en="NIO",
                    category="ai_assistant",
                    feature_name_en="NOMI GPT",
                )
            ],
        ]
        db_mocks["get_vehicles_by_brand"].side_effect = [[_make_vehicle()], []]

        result = await comparison.get_brand_comparison(["BYD", "NIO"])

        assert result["brands_not_found"] == []
        byd = result["brands"]["BYD"]
        assert byd["brand_info"] == {
            "name_en": "BYD",
            "name_zh": "比亚迪",
            "parent_group": "BYD",
            "ev_focus": False,
        }
        assert byd["vehicle_count"] == 1
        assert byd["vehicles"] == [
            {
                "model_name_en": "Seal",
                "segment": "sedan",
                "powertrain": "BEV",
                "price_range_cny": "150,000-200,000",
            }
        ]
        assert byd["feature_count"] == 1
        assert byd["categories_covered"] == ["adas"]
        nio = result["brands"]["NIO"]
        assert nio["brand_info"]["ev_focus"] is True
        assert nio["vehicle_count"] == 0
        assert nio["categories_covered"] == ["ai_assistant"]

    async def test_handles_brand_not_found(self, db_mocks: dict[str, AsyncMock]) -> None:
        """Unknown brands land in brands_not_found and skip further fetches."""
        db_mocks["get_brand_by_name_en"].side_effect = [_make_brand(), None]
        db_mocks["get_features_by_brand"].return_value = [_make_feature()]
        db_mocks["get_vehicles_by_brand"].return_value = []

        result = await comparison.get_brand_comparison(["BYD", "Tesla"])

        assert result["brands_not_found"] == ["Tesla"]
        assert list(result["brands"]) == ["BYD"]
        db_mocks["get_features_by_brand"].assert_awaited_once_with("BYD")
        db_mocks["get_vehicles_by_brand"].assert_awaited_once_with("BYD")

    async def test_category_comparison_counts_correct(self, db_mocks: dict[str, AsyncMock]) -> None:
        """Every found brand gets a count in every populated category."""
        db_mocks["get_brand_by_name_en"].side_effect = [
            _make_brand(),
            _make_brand(id="brand-2", name_en="NIO"),
        ]
        db_mocks["get_features_by_brand"].side_effect = [
            [
                _make_feature(id="feat-1", category="adas"),
                _make_feature(id="feat-2", category="adas", feature_name_en="Highway NOA"),
                _make_feature(id="feat-3", category="ota", feature_name_en="Whole-vehicle OTA"),
            ],
            [_make_feature(id="feat-4", brand_name_en="NIO", category="adas")],
        ]

        result = await comparison.get_brand_comparison(["BYD", "NIO"])

        assert result["category_comparison"] == {
            "adas": {"BYD": 2, "NIO": 1},
            "ota": {"BYD": 1, "NIO": 0},
        }

    async def test_single_brand_works(self, db_mocks: dict[str, AsyncMock]) -> None:
        """A single-brand comparison still builds the full structure."""
        db_mocks["get_brand_by_name_en"].return_value = _make_brand()
        db_mocks["get_features_by_brand"].return_value = [_make_feature()]
        db_mocks["get_vehicles_by_brand"].return_value = [_make_vehicle()]

        result = await comparison.get_brand_comparison(["BYD"])

        assert list(result["brands"]) == ["BYD"]
        assert result["category_comparison"] == {"adas": {"BYD": 1}}
        assert result["brands_not_found"] == []

    async def test_empty_features_for_brand(self, db_mocks: dict[str, AsyncMock]) -> None:
        """A brand with no features gets zero counts and empty groupings."""
        db_mocks["get_brand_by_name_en"].return_value = _make_brand()
        db_mocks["get_features_by_brand"].return_value = []
        db_mocks["get_vehicles_by_brand"].return_value = [_make_vehicle()]

        result = await comparison.get_brand_comparison(["BYD"])

        byd = result["brands"]["BYD"]
        assert byd["feature_count"] == 0
        assert byd["features_by_category"] == {}
        assert byd["categories_covered"] == []
        assert result["category_comparison"] == {}


class TestGetPriceFeatureAnalysis:
    """get_price_feature_analysis groups vehicles into CNY price tiers."""

    async def test_groups_vehicles_into_tiers(self, db_mocks: dict[str, AsyncMock]) -> None:
        """Vehicles land in budget/mid/premium/luxury by price lower bound."""
        db_mocks["get_all_vehicles"].return_value = [
            _make_vehicle(id="veh-1", model_name_en="Seagull", price_range_cny="80,000-100,000"),
            _make_vehicle(id="veh-2", model_name_en="Seal", price_range_cny="200,000-260,000"),
            _make_vehicle(
                id="veh-3",
                brand_name_en="NIO",
                model_name_en="ET5",
                price_range_cny="320,000-380,000",
            ),
            _make_vehicle(
                id="veh-4",
                brand_name_en="NIO",
                model_name_en="ET9",
                price_range_cny="780,000-810,000",
            ),
        ]
        db_mocks["get_all_features"].return_value = [
            _make_feature(id="feat-1", category="adas"),
            _make_feature(id="feat-2", category="ota", feature_name_en="Whole-vehicle OTA"),
        ]

        result = await comparison.get_price_feature_analysis()

        tiers = result["tiers"]
        assert tiers["budget"]["vehicle_count"] == 1
        assert tiers["mid"]["vehicle_count"] == 1
        assert tiers["premium"]["vehicle_count"] == 1
        assert tiers["luxury"]["vehicle_count"] == 1
        assert tiers["unknown"]["vehicle_count"] == 0
        assert tiers["budget"]["vehicles"] == [
            {"brand": "BYD", "model": "Seagull", "price_range_cny": "80,000-100,000"}
        ]
        assert tiers["mid"]["avg_features_by_category"] == {
            "adas": pytest.approx(1.0),
            "ota": pytest.approx(1.0),
        }
        assert tiers["mid"]["total_avg_features"] == pytest.approx(2.0)
        assert tiers["premium"]["avg_features_by_category"] == {}
        assert result["filters_applied"] == {"segment": None}

    async def test_filters_by_segment(self, db_mocks: dict[str, AsyncMock]) -> None:
        """The segment filter is pushed down to the vehicles query."""
        db_mocks["get_all_vehicles"].return_value = [_make_vehicle(segment="SUV")]

        result = await comparison.get_price_feature_analysis(segment="SUV")

        db_mocks["get_all_vehicles"].assert_awaited_once_with("SUV")
        assert result["filters_applied"] == {"segment": "SUV"}
        assert result["tiers"]["mid"]["vehicle_count"] == 1

    async def test_handles_missing_price_data(self, db_mocks: dict[str, AsyncMock]) -> None:
        """Vehicles without a parseable price land in the unknown tier."""
        db_mocks["get_all_vehicles"].return_value = [
            _make_vehicle(id="veh-1", price_range_cny=None),
            _make_vehicle(id="veh-2", model_name_en="Han", price_range_cny="TBD"),
        ]

        result = await comparison.get_price_feature_analysis()

        assert result["tiers"]["unknown"]["vehicle_count"] == 2
        assert result["tiers"]["unknown"]["vehicles"] == [
            {"brand": "BYD", "model": "Seal", "price_range_cny": None},
            {"brand": "BYD", "model": "Han", "price_range_cny": "TBD"},
        ]

    async def test_parses_price_formats(self, db_mocks: dict[str, AsyncMock]) -> None:
        """Comma-grouped and plain price strings both classify by lower bound."""
        db_mocks["get_all_vehicles"].return_value = [
            _make_vehicle(id="veh-1", price_range_cny="150,000-200,000"),
            _make_vehicle(id="veh-2", model_name_en="Han", price_range_cny="150000-200000"),
        ]

        result = await comparison.get_price_feature_analysis()

        assert result["tiers"]["mid"]["vehicle_count"] == 2
        assert result["tiers"]["unknown"]["vehicle_count"] == 0

    async def test_empty_vehicles_returns_empty_tiers(self, db_mocks: dict[str, AsyncMock]) -> None:
        """No vehicles yields every tier key with zero counts."""
        result = await comparison.get_price_feature_analysis()

        assert set(result["tiers"]) == {"budget", "mid", "premium", "luxury", "unknown"}
        for tier_stats in result["tiers"].values():
            assert tier_stats == {
                "vehicle_count": 0,
                "vehicles": [],
                "avg_features_by_category": {},
                "total_avg_features": 0.0,
            }
