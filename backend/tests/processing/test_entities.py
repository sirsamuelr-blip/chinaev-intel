"""Tests for the entity promotion module.

All external calls are mocked — the Firestore helpers are monkeypatched on
the entities module (matching the ``test_pipeline`` pattern) and the Claude
fallback is stubbed via the ``call_claude`` name entities imports from
``processing.utils``. Alias resolution tests use
an in-test dictionary; ``load_aliases`` tests read the real JSON file.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from processing import entities

SAMPLE_ALIASES: dict[str, Any] = {
    "aliases": {
        "BYD": "BYD",
        "BYD Auto": "BYD",
        "比亚迪": "BYD",
        "XPENG": "XPENG",
        "小鹏": "XPENG",
        "Li Auto": "Li Auto",
        "理想": "Li Auto",
    },
    "brands": {
        "BYD": {"nameEn": "BYD", "nameZh": "比亚迪", "parentGroup": "BYD", "evFocus": False},
        "XPENG": {"nameEn": "XPENG", "nameZh": "小鹏", "parentGroup": "XPENG", "evFocus": True},
        "Li Auto": {
            "nameEn": "Li Auto",
            "nameZh": "理想汽车",
            "parentGroup": "Li Auto",
            "evFocus": True,
        },
    },
}

SAMPLE_ARTICLE: dict[str, Any] = {
    "id": "article-1",
    "brandsMentioned": ["BYD", "小鹏"],
    "vehiclesMentioned": ["BYD Seal"],
    "featuresExtracted": [
        {
            "feature_name": "City NOA",
            "category": "adas",
            "description": "Urban navigate-on-autopilot",
            "supplier": None,
            "is_new": True,
        },
        {
            "feature_name": "Voice assistant",
            "category": "ai_assistant",
            "description": "Existing voice assistant",
            "supplier": None,
            "is_new": False,
        },
    ],
}


def _await_arg(mock: AsyncMock) -> Any:
    """Return the single positional argument of the mock's last await."""
    assert mock.await_args is not None
    (arg,) = mock.await_args.args
    return arg


@pytest.fixture
def db_mocks(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Mock the Firestore helpers, the Claude fallback, and the alias cache."""
    mocks = {
        "get_brand_by_name_en": AsyncMock(return_value=None),
        "upsert_brand": AsyncMock(return_value="brand-1"),
        "upsert_vehicle": AsyncMock(return_value="vehicle-1"),
        "upsert_feature": AsyncMock(return_value="feature-1"),
        "call_claude": AsyncMock(return_value="null"),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(entities, name, mock)
    monkeypatch.setattr(entities, "_aliases_cache", SAMPLE_ALIASES)
    return mocks


@pytest.fixture
def real_aliases(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Load the real brand_aliases.json, bypassing the module cache."""
    monkeypatch.setattr(entities, "_aliases_cache", None)
    return entities.load_aliases()


class TestLoadAliases:
    """load_aliases reads and caches the real dictionary file."""

    def test_load_aliases_returns_expected_structure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The real JSON file has aliases and brands maps that cross-reference."""
        monkeypatch.setattr(entities, "_aliases_cache", None)

        result = entities.load_aliases()

        assert set(result.keys()) == {"aliases", "brands"}
        assert result["aliases"]["比亚迪"] == "BYD"
        assert result["aliases"]["蔚来"] == "NIO"
        assert result["brands"]["BYD"]["parentGroup"] == "BYD"
        # Every alias target must be a canonical brand with full metadata.
        assert set(result["aliases"].values()) <= set(result["brands"].keys())
        for metadata in result["brands"].values():
            assert set(metadata.keys()) == {"nameEn", "nameZh", "parentGroup", "evFocus"}

    def test_load_aliases_caches_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A second call returns the cached object without re-reading the file."""
        monkeypatch.setattr(entities, "_aliases_cache", None)

        first = entities.load_aliases()

        assert entities.load_aliases() is first


class TestResolveBrandName:
    """resolve_brand_name maps variants to canonical English names."""

    def test_resolve_brand_name_exact_match(self) -> None:
        """A canonical name resolves to itself."""
        assert entities.resolve_brand_name("BYD", SAMPLE_ALIASES) == "BYD"

    def test_resolve_brand_name_case_insensitive(self) -> None:
        """Lookup ignores case and surrounding whitespace."""
        assert entities.resolve_brand_name("byd auto", SAMPLE_ALIASES) == "BYD"
        assert entities.resolve_brand_name("XPeng", SAMPLE_ALIASES) == "XPENG"
        assert entities.resolve_brand_name("  li auto  ", SAMPLE_ALIASES) == "Li Auto"

    def test_resolve_brand_name_chinese(self) -> None:
        """Chinese alias names resolve to the canonical English name."""
        assert entities.resolve_brand_name("比亚迪", SAMPLE_ALIASES) == "BYD"
        assert entities.resolve_brand_name("小鹏", SAMPLE_ALIASES) == "XPENG"

    def test_resolve_brand_name_unknown_returns_none(self) -> None:
        """A name not in the dictionary returns None."""
        assert entities.resolve_brand_name("Tesla", SAMPLE_ALIASES) is None


class TestResolveBrandWithFallback:
    """resolve_brand_with_fallback tries the dictionary, then Sonnet."""

    async def test_resolve_brand_with_fallback_uses_dictionary_first(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """A dictionary hit never calls the LLM."""
        result = await entities.resolve_brand_with_fallback("比亚迪", SAMPLE_ALIASES)

        assert result == "BYD"
        db_mocks["call_claude"].assert_not_awaited()

    async def test_resolve_brand_with_fallback_llm_success(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """A Sonnet resolution returns the canonical name."""
        db_mocks["call_claude"].return_value = json.dumps(
            {"name_en": "Onvo", "name_zh": "乐道", "parent_group": "NIO"}
        )

        assert await entities.resolve_brand_with_fallback("乐道", SAMPLE_ALIASES) == "Onvo"

    async def test_resolve_brand_with_fallback_llm_null(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """Sonnet answering null (not a known brand) returns None."""
        assert await entities.resolve_brand_with_fallback("Acme Corp", SAMPLE_ALIASES) is None

    async def test_resolve_brand_with_fallback_llm_failure(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """An exhausted or unparseable LLM call returns None."""
        db_mocks["call_claude"].return_value = None

        assert await entities.resolve_brand_with_fallback("Mystery", SAMPLE_ALIASES) is None


class TestPromoteBrands:
    """promote_brands resolves and upserts brand names."""

    async def test_promote_brands_creates_new_brand(self, db_mocks: dict[str, AsyncMock]) -> None:
        """A resolved brand is upserted with its dictionary metadata."""
        result = await entities.promote_brands(["BYD"], SAMPLE_ALIASES)

        assert result == {"BYD": "brand-1"}
        db_mocks["upsert_brand"].assert_awaited_once_with(
            {"name_en": "BYD", "name_zh": "比亚迪", "parent_group": "BYD", "ev_focus": False}
        )

    async def test_promote_brands_updates_existing_brand(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """An existing brand's doc ID from the upsert is returned in the map."""
        db_mocks["upsert_brand"].return_value = "brand-existing"

        result = await entities.promote_brands(["XPENG"], SAMPLE_ALIASES)

        assert result == {"XPENG": "brand-existing"}
        db_mocks["upsert_brand"].assert_awaited_once()

    async def test_promote_brands_resolves_alias(self, db_mocks: dict[str, AsyncMock]) -> None:
        """A Chinese alias upserts under the canonical name, keyed by the original."""
        result = await entities.promote_brands(["比亚迪"], SAMPLE_ALIASES)

        assert result == {"比亚迪": "brand-1"}
        assert _await_arg(db_mocks["upsert_brand"])["name_en"] == "BYD"

    async def test_promote_brands_skips_unknown_brand(self, db_mocks: dict[str, AsyncMock]) -> None:
        """A name the dictionary and fallback both miss is skipped."""
        result = await entities.promote_brands(["Acme Corp"], SAMPLE_ALIASES)

        assert result == {}
        db_mocks["upsert_brand"].assert_not_awaited()

    async def test_promote_brands_handles_empty_list(self, db_mocks: dict[str, AsyncMock]) -> None:
        """An empty input returns an empty map without any writes."""
        assert await entities.promote_brands([], SAMPLE_ALIASES) == {}
        db_mocks["upsert_brand"].assert_not_awaited()


class TestPromoteVehicles:
    """promote_vehicles parses brand + model and upserts vehicles."""

    async def test_promote_vehicles_creates_new_vehicle(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """A vehicle with a resolvable brand prefix is upserted."""
        result = await entities.promote_vehicles(["BYD Seal"], {"BYD": "brand-1"}, SAMPLE_ALIASES)

        assert result == {"BYD Seal": "vehicle-1"}
        db_mocks["upsert_vehicle"].assert_awaited_once_with(
            {"brand_id": "brand-1", "brand_name_en": "BYD", "model_name_en": "Seal"}
        )

    async def test_promote_vehicles_parses_brand_and_model(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """Multi-word brand prefixes match longest-first."""
        await entities.promote_vehicles(["Li Auto L9 Pro"], {"Li Auto": "brand-li"}, SAMPLE_ALIASES)

        vehicle_data = _await_arg(db_mocks["upsert_vehicle"])
        assert vehicle_data["brand_name_en"] == "Li Auto"
        assert vehicle_data["model_name_en"] == "L9 Pro"
        assert vehicle_data["brand_id"] == "brand-li"

    async def test_promote_vehicles_skips_unresolvable_brand(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """A vehicle whose prefix matches no known brand is skipped."""
        result = await entities.promote_vehicles(
            ["Tesla Model 3", "Han EV"], {"BYD": "brand-1"}, SAMPLE_ALIASES
        )

        assert result == {}
        db_mocks["upsert_vehicle"].assert_not_awaited()

    async def test_promote_vehicles_falls_back_to_firestore_brand_lookup(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """A brand missing from brand_map is looked up in Firestore by name."""
        db_mocks["get_brand_by_name_en"].return_value = {"id": "brand-x", "name_en": "XPENG"}

        result = await entities.promote_vehicles(["XPENG G6"], {}, SAMPLE_ALIASES)

        assert result == {"XPENG G6": "vehicle-1"}
        db_mocks["get_brand_by_name_en"].assert_awaited_once_with("XPENG")

    async def test_promote_vehicles_handles_empty_list(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """An empty input returns an empty map without any writes."""
        assert await entities.promote_vehicles([], {"BYD": "brand-1"}, SAMPLE_ALIASES) == {}
        db_mocks["upsert_vehicle"].assert_not_awaited()


class TestPromoteFeatures:
    """promote_features upserts new features linked to brand and vehicle."""

    async def test_promote_features_creates_new_feature(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """A feature with is_new True is upserted and its doc ID returned."""
        features = [SAMPLE_ARTICLE["featuresExtracted"][0]]

        result = await entities.promote_features(features, {"BYD": "brand-1"}, {})

        assert result == ["feature-1"]
        feature_data = _await_arg(db_mocks["upsert_feature"])
        assert feature_data["feature_name_en"] == "City NOA"
        assert feature_data["category"] == "adas"
        assert feature_data["launch_type"] == "new"

    async def test_promote_features_skips_existing_feature(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """A feature with is_new False is not promoted."""
        features = [SAMPLE_ARTICLE["featuresExtracted"][1]]

        result = await entities.promote_features(features, {"BYD": "brand-1"}, {})

        assert result == []
        db_mocks["upsert_feature"].assert_not_awaited()

    async def test_promote_features_links_brand_and_vehicle(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """The feature links to the first resolved brand and vehicle."""
        features = [SAMPLE_ARTICLE["featuresExtracted"][0]]

        await entities.promote_features(features, {"比亚迪": "brand-1"}, {"BYD Seal": "vehicle-1"})

        feature_data = _await_arg(db_mocks["upsert_feature"])
        assert feature_data["brand_id"] == "brand-1"
        assert feature_data["brand_name_en"] == "BYD"
        assert feature_data["vehicle_id"] == "vehicle-1"
        assert feature_data["vehicle_model_name"] == "Seal"

    async def test_promote_features_sets_first_seen_date_on_new_only(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """first_seen_date is delegated to the Firestore layer, never passed here."""
        features = [SAMPLE_ARTICLE["featuresExtracted"][0]]

        await entities.promote_features(features, {"BYD": "brand-1"}, {})

        feature_data = _await_arg(db_mocks["upsert_feature"])
        assert "first_seen_date" not in feature_data
        assert "firstSeenDate" not in feature_data

    async def test_promote_features_handles_empty_list(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """An empty input returns an empty list without any writes."""
        assert await entities.promote_features([], {"BYD": "brand-1"}, {}) == []
        db_mocks["upsert_feature"].assert_not_awaited()

    async def test_promote_features_skips_all_without_brand(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """With no resolved brand to link, new features are skipped."""
        features = [SAMPLE_ARTICLE["featuresExtracted"][0]]

        assert await entities.promote_features(features, {}, {}) == []
        db_mocks["upsert_feature"].assert_not_awaited()


class TestPromoteEntitiesFromArticle:
    """promote_entities_from_article orchestrates the full promotion flow."""

    async def test_promote_entities_from_article_full_flow(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """Brands, vehicles, and new features are all promoted and counted."""
        summary = await entities.promote_entities_from_article(SAMPLE_ARTICLE)

        assert summary == {
            "brands_promoted": 2,
            "vehicles_promoted": 1,
            "features_promoted": 1,
        }
        assert db_mocks["upsert_brand"].await_count == 2
        db_mocks["upsert_vehicle"].assert_awaited_once()
        db_mocks["upsert_feature"].assert_awaited_once()

    async def test_promote_entities_from_article_missing_fields(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """A doc missing brandsMentioned skips promotion steps without erroring."""
        article = {"id": "article-2", "featuresExtracted": SAMPLE_ARTICLE["featuresExtracted"]}

        summary = await entities.promote_entities_from_article(article)

        assert summary == {
            "brands_promoted": 0,
            "vehicles_promoted": 0,
            "features_promoted": 0,
        }
        db_mocks["upsert_brand"].assert_not_awaited()
        db_mocks["upsert_feature"].assert_not_awaited()

    async def test_promote_entities_from_article_single_failure_doesnt_crash(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """One brand upsert failing does not abort the remaining entities."""
        db_mocks["upsert_brand"].side_effect = [RuntimeError("firestore down"), "brand-2"]

        summary = await entities.promote_entities_from_article(SAMPLE_ARTICLE)

        assert summary["brands_promoted"] == 1
        assert db_mocks["upsert_brand"].await_count == 2
        assert summary["features_promoted"] == 1


class TestNewBrandAliases:
    """The real alias dictionary resolves the newly added sub-brands."""

    @pytest.mark.parametrize(
        ("alias", "canonical", "parent_group"),
        [
            ("仰望", "Yangwang", "BYD"),
            ("Yangwang", "Yangwang", "BYD"),
            ("腾势", "Denza", "BYD"),
            ("方程豹", "Fang Cheng Bao", "BYD"),
            ("Leopard", "Fang Cheng Bao", "BYD"),
            ("银河", "Galaxy", "Geely"),
            ("智界", "Luxeed", "Chery"),
            ("Zhijie", "Luxeed", "Chery"),
            ("乐道", "Onvo", "NIO"),
            ("Ledao", "Onvo", "NIO"),
            ("萤火虫", "Firefly", "NIO"),
            ("极狐", "Arcfox", "BAIC"),
            ("泰钽", "Taitanium", "BAIC"),
            ("至境", "Enclave", "Buick"),
            ("Kona", "Hyundai", "Hyundai"),
            ("IONIQ", "Hyundai", "Hyundai"),
            ("GV90", "Genesis", "Hyundai"),
            ("S500", "Mercedes-Benz", "Mercedes-Benz"),
        ],
    )
    def test_new_alias_resolves_to_canonical_and_parent(
        self,
        real_aliases: dict[str, Any],
        alias: str,
        canonical: str,
        parent_group: str,
    ) -> None:
        """Each new alias resolves to its canonical brand with the right parent."""
        assert entities.resolve_brand_name(alias, real_aliases) == canonical
        assert real_aliases["brands"][canonical]["parentGroup"] == parent_group

    def test_sub_brand_vehicle_splits_to_parent_sub_brand(
        self, real_aliases: dict[str, Any]
    ) -> None:
        """A sub-brand vehicle name splits into the sub-brand and model."""
        assert entities._split_vehicle_name("Yangwang U8", real_aliases) == ("Yangwang", "U8")
        assert entities._split_vehicle_name("Denza Z9GT", real_aliases) == ("Denza", "Z9GT")


class TestNonAutomotiveFiltering:
    """Known non-automotive false positives are filtered before resolution."""

    @pytest.mark.parametrize(
        "name",
        [
            "iQIYI",
            "爱奇艺",
            "Honor",
            "荣耀",
            "Flyme",
            "元镜",
            "Yuan Mirror",
            "国家大剧院",
            "National Centre for the Performing Arts",
        ],
    )
    def test_is_non_automotive_brand_true(self, name: str) -> None:
        """Every listed false positive is recognized."""
        assert entities.is_non_automotive_brand(name) is True

    def test_is_non_automotive_brand_case_insensitive(self) -> None:
        """Matching ignores case and surrounding whitespace."""
        assert entities.is_non_automotive_brand("  iqiyi ") is True

    def test_is_non_automotive_brand_false_for_real_brand(self) -> None:
        """A real automaker is not filtered."""
        assert entities.is_non_automotive_brand("BYD") is False

    async def test_promote_brands_skips_non_automotive_silently(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """False positives are dropped with no upsert and no Sonnet call."""
        result = await entities.promote_brands(["iQIYI", "国家大剧院"], SAMPLE_ALIASES)

        assert result == {}
        db_mocks["call_claude"].assert_not_awaited()
        db_mocks["upsert_brand"].assert_not_awaited()

    async def test_promote_brands_filters_non_automotive_but_keeps_real(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """A real brand alongside a false positive still promotes."""
        result = await entities.promote_brands(["爱奇艺", "BYD"], SAMPLE_ALIASES)

        assert result == {"BYD": "brand-1"}
        db_mocks["call_claude"].assert_not_awaited()


class TestBrandResolver:
    """BrandResolver memoizes and caps the Sonnet fallback within a run."""

    async def test_dictionary_hit_never_calls_sonnet(self, db_mocks: dict[str, AsyncMock]) -> None:
        """A name in the alias dictionary resolves without the LLM."""
        resolver = entities.BrandResolver(SAMPLE_ALIASES)

        assert await resolver.resolve("比亚迪") == "BYD"
        db_mocks["call_claude"].assert_not_awaited()

    async def test_caches_successful_resolution_within_run(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """The same unknown name resolves once; the second lookup is cached."""
        db_mocks["call_claude"].return_value = json.dumps(
            {"name_en": "Onvo", "name_zh": "乐道", "parent_group": "NIO"}
        )
        resolver = entities.BrandResolver(SAMPLE_ALIASES)

        first = await resolver.resolve("乐道")
        second = await resolver.resolve("乐道")

        assert first == second == "Onvo"
        db_mocks["call_claude"].assert_awaited_once()

    async def test_caches_failed_resolution_within_run(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """A name that fails resolution is not retried within the run."""
        db_mocks["call_claude"].return_value = "null"
        resolver = entities.BrandResolver(SAMPLE_ALIASES)

        assert await resolver.resolve("Acme Corp") is None
        assert await resolver.resolve("Acme Corp") is None
        db_mocks["call_claude"].assert_awaited_once()

    async def test_cache_shared_across_promote_brands_calls(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """One resolver across two article promotions dedups the Sonnet call."""
        db_mocks["call_claude"].return_value = json.dumps(
            {"name_en": "Onvo", "name_zh": "乐道", "parent_group": "NIO"}
        )
        resolver = entities.BrandResolver(SAMPLE_ALIASES)

        await entities.promote_brands(["乐道"], SAMPLE_ALIASES, resolver)
        await entities.promote_brands(["乐道"], SAMPLE_ALIASES, resolver)

        db_mocks["call_claude"].assert_awaited_once()

    async def test_caps_sonnet_calls_per_run(
        self, db_mocks: dict[str, AsyncMock], caplog: pytest.LogCaptureFixture
    ) -> None:
        """No more than MAX_SONNET_BRAND_RESOLUTIONS Sonnet calls happen per run."""
        db_mocks["call_claude"].return_value = json.dumps(
            {"name_en": "X", "name_zh": None, "parent_group": None}
        )
        resolver = entities.BrandResolver(SAMPLE_ALIASES)
        cap = entities.MAX_SONNET_BRAND_RESOLUTIONS

        with caplog.at_level(logging.INFO, logger="processing.entities"):
            results = [await resolver.resolve(f"Unknown Brand {i}") for i in range(cap + 5)]

        assert db_mocks["call_claude"].await_count == cap
        assert results[:cap] == ["X"] * cap
        assert results[cap:] == [None] * 5
        cap_logs = [r for r in caplog.records if "cap reached" in r.getMessage()]
        assert len(cap_logs) == 1

    async def test_salvages_json_with_trailing_commentary(
        self, db_mocks: dict[str, AsyncMock]
    ) -> None:
        """A valid object followed by prose is salvaged rather than rejected."""
        db_mocks["call_claude"].return_value = (
            '{"name_en": "Onvo", "name_zh": "乐道", "parent_group": "NIO"}\n\n'
            "Note: Onvo is NIO's mass-market sub-brand."
        )
        resolver = entities.BrandResolver(SAMPLE_ALIASES)

        assert await resolver.resolve("乐道") == "Onvo"

    async def test_warns_on_unparseable_response(
        self, db_mocks: dict[str, AsyncMock], caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unparseable response returns None and warns with the brand name."""
        db_mocks["call_claude"].return_value = "Sorry, I cannot determine that."
        resolver = entities.BrandResolver(SAMPLE_ALIASES)

        with caplog.at_level(logging.WARNING, logger="processing.entities"):
            assert await resolver.resolve("乐道") is None

        warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING and "乐道" in record.getMessage()
        ]
        assert warnings

    async def test_warns_on_object_missing_name_en(
        self, db_mocks: dict[str, AsyncMock], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A parseable object without name_en returns None and warns."""
        db_mocks["call_claude"].return_value = json.dumps({"name_zh": "乐道"})
        resolver = entities.BrandResolver(SAMPLE_ALIASES)

        with caplog.at_level(logging.WARNING, logger="processing.entities"):
            assert await resolver.resolve("乐道") is None

        assert any("missing name_en" in record.getMessage() for record in caplog.records)
