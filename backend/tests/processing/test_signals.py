"""Tests for the signal detection module.

Stage 1 trigger rules are pure functions tested directly on article dicts
(camelCase keys, Firestore doc shape). Stage 2 and the entry point mock all
external calls: the Anthropic client via ``anthropic.AsyncAnthropic``
(matching the ``test_pipeline`` pattern) and the Firestore helpers via
monkeypatch on the signals module (matching ``test_entities``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anthropic
import pytest

from config import settings
from processing import signals

ARTICLE_NEW_FEATURE: dict[str, Any] = {
    "id": "article-launch",
    "titleEn": "BYD rolls out city NOA to the Seal",
    "bodyEn": "BYD said the Seal sedan gains urban navigate-on-autopilot in a staged rollout.",
    "contentType": "news",
    "brandsMentioned": ["BYD"],
    "vehiclesMentioned": ["BYD Seal"],
    "featuresExtracted": [
        {
            "feature_name": "City NOA",
            "category": "adas",
            "description": "Urban navigate-on-autopilot for dense city streets",
            "supplier": None,
            "is_new": True,
        }
    ],
}

ARTICLE_AI_KEYWORD: dict[str, Any] = {
    "id": "article-ai",
    "titleEn": "XPeng taps a new provider for its cockpit assistant",
    "bodyEn": "XPeng will integrate the DeepSeek reasoning stack into its voice assistant.",
    "contentType": "news",
    "brandsMentioned": ["XPeng"],
    "featuresExtracted": [],
}

ARTICLE_MULTI_SIGNAL: dict[str, Any] = {
    "id": "article-multi",
    "titleEn": "NIO launches NOMI Agent",
    "bodyEn": "NIO introduced an upgraded in-car assistant.",
    "contentType": "news",
    "brandsMentioned": ["NIO"],
    "featuresExtracted": [
        {
            "feature_name": "NOMI Agent",
            "category": "ai_assistant",
            "description": "Agentic in-car voice assistant",
            "supplier": None,
            "is_new": True,
        }
    ],
}

ARTICLE_IRRELEVANT: dict[str, Any] = {
    "id": "article-boring",
    "titleEn": "Weekly sales roundup",
    "bodyEn": "Sedan sales rose four percent week over week.",
    "contentType": "review",
    "brandsMentioned": ["BYD"],
    "featuresExtracted": [],
}

ARTICLE_DUPLICATE: dict[str, Any] = {
    **ARTICLE_NEW_FEATURE,
    "id": "article-dup",
    "isDuplicate": True,
}

EXISTING_FEATURES: list[dict[str, Any]] = [
    {
        "id": "feature-xpeng-ngp",
        "brand_id": "brand-xpeng",
        "brand_name_en": "XPENG",
        "feature_name_en": "City NGP",
        "category": "adas",
    }
]

SAMPLE_CANDIDATE: dict[str, Any] = {
    "signal_type": "new_feature_launch",
    "source_article_ids": ["article-launch"],
    "brands_mentioned": ["BYD"],
    "features_mentioned": ["City NOA"],
    "trigger_data": {"feature_name": "City NOA", "category": "adas", "description": "Urban NOA"},
}

SAMPLE_NARRATIVE: dict[str, Any] = {
    "title": "BYD brings city NOA to the mass-market Seal",
    "summary": "BYD is rolling urban navigate-on-autopilot out to its volume sedan.",
    "implications_for_western_oems": "ADAS price points are dropping below Western flagships.",
    "competitive_impact_score": 7,
}


def _make_client(response_text: str) -> MagicMock:
    """Build a mock Anthropic client whose response contains ``response_text``."""
    block = MagicMock()
    block.type = "text"
    block.text = response_text
    response = MagicMock()
    response.content = [block]
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=response)
    return client


def _make_failing_client(error: Exception) -> MagicMock:
    """Build a mock Anthropic client whose API call always raises ``error``."""
    client = MagicMock()
    client.messages.create = AsyncMock(side_effect=error)
    return client


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace asyncio.sleep so retry backoff runs instantly."""
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    return sleep


class TestCheckNewFeatureLaunch:
    """check_new_feature_launch fires on featuresExtracted items with is_new."""

    def test_fires_on_new_feature(self) -> None:
        """An article with one new feature yields one fully-shaped candidate."""
        result = signals.check_new_feature_launch(ARTICLE_NEW_FEATURE)

        assert len(result) == 1
        candidate = result[0]
        assert candidate["signal_type"] == "new_feature_launch"
        assert candidate["source_article_ids"] == ["article-launch"]
        assert candidate["brands_mentioned"] == ["BYD"]
        assert candidate["features_mentioned"] == ["City NOA"]
        assert candidate["trigger_data"] == {
            "feature_name": "City NOA",
            "category": "adas",
            "description": "Urban navigate-on-autopilot for dense city streets",
        }

    def test_returns_empty_no_new_features(self) -> None:
        """Features with is_new False never fire."""
        article = {
            **ARTICLE_NEW_FEATURE,
            "featuresExtracted": [
                {"feature_name": "City NOA", "category": "adas", "is_new": False}
            ],
        }

        assert signals.check_new_feature_launch(article) == []

    def test_returns_empty_no_features(self) -> None:
        """An empty featuresExtracted list never fires."""
        assert (
            signals.check_new_feature_launch({**ARTICLE_NEW_FEATURE, "featuresExtracted": []}) == []
        )

    def test_returns_empty_missing_field(self) -> None:
        """An article without a featuresExtracted key never fires."""
        assert signals.check_new_feature_launch({"id": "a", "brandsMentioned": ["BYD"]}) == []

    def test_multiple_new_features_returns_multiple(self) -> None:
        """Two new features yield two candidates."""
        article = {
            **ARTICLE_NEW_FEATURE,
            "featuresExtracted": [
                {"feature_name": "City NOA", "category": "adas", "is_new": True},
                {"feature_name": "Valet parking", "category": "adas", "is_new": True},
            ],
        }

        result = signals.check_new_feature_launch(article)

        assert len(result) == 2
        assert [c["features_mentioned"] for c in result] == [["City NOA"], ["Valet parking"]]


class TestCheckFeatureTrickleDown:
    """check_feature_trickle_down fires when another brand already has the category."""

    def test_fires_when_existing_feature_matches(self) -> None:
        """Same category under a different brand yields a candidate."""
        result = signals.check_feature_trickle_down(ARTICLE_NEW_FEATURE, EXISTING_FEATURES)

        assert len(result) == 1
        candidate = result[0]
        assert candidate["signal_type"] == "feature_trickle_down"
        assert candidate["trigger_data"] == {
            "new_feature_name": "City NOA",
            "new_feature_category": "adas",
            "existing_feature_name": "City NGP",
            "existing_brand": "XPENG",
        }

    def test_returns_empty_no_existing_features(self) -> None:
        """An empty existing-features list never fires."""
        assert signals.check_feature_trickle_down(ARTICLE_NEW_FEATURE, []) == []

    def test_returns_empty_same_brand(self) -> None:
        """An existing feature from a brand mentioned in the article is not trickle-down."""
        same_brand = [{**EXISTING_FEATURES[0], "brand_name_en": "BYD"}]

        assert signals.check_feature_trickle_down(ARTICLE_NEW_FEATURE, same_brand) == []

    def test_returns_empty_different_category(self) -> None:
        """An existing feature in a different category never fires."""
        other_category = [{**EXISTING_FEATURES[0], "category": "ota"}]

        assert signals.check_feature_trickle_down(ARTICLE_NEW_FEATURE, other_category) == []


class TestCheckAiIntegration:
    """check_ai_integration fires on new AI features or AI-provider keywords."""

    def test_fires_on_ai_assistant_feature(self) -> None:
        """A new ai_assistant feature fires the feature branch."""
        result = signals.check_ai_integration(ARTICLE_MULTI_SIGNAL)

        assert len(result) == 1
        candidate = result[0]
        assert candidate["signal_type"] == "ai_integration"
        assert candidate["features_mentioned"] == ["NOMI Agent"]
        assert candidate["trigger_data"] == {
            "trigger_reason": "new_ai_feature",
            "matched_keywords": [],
            "feature_name": "NOMI Agent",
        }

    def test_fires_on_keyword_match_in_title(self) -> None:
        """An AI-provider keyword in titleEn fires the keyword branch."""
        article = {
            "id": "a",
            "titleEn": "Geely puts a large language model in the cockpit",
            "bodyEn": "",
            "contentType": "news",
            "brandsMentioned": ["Geely"],
        }

        result = signals.check_ai_integration(article)

        assert len(result) == 1
        assert result[0]["trigger_data"]["trigger_reason"] == "ai_keyword_match"
        assert "large language model" in result[0]["trigger_data"]["matched_keywords"]
        assert result[0]["trigger_data"]["feature_name"] is None

    def test_fires_on_keyword_match_in_body(self) -> None:
        """An AI-provider keyword in bodyEn fires the keyword branch."""
        result = signals.check_ai_integration(ARTICLE_AI_KEYWORD)

        assert len(result) == 1
        assert "deepseek" in result[0]["trigger_data"]["matched_keywords"]

    def test_no_duplicate_when_both_match(self) -> None:
        """A new AI feature plus a keyword match still yields one candidate."""
        article = {
            **ARTICLE_MULTI_SIGNAL,
            "bodyEn": "NIO built the NOMI Agent on a DeepSeek foundation model.",
        }

        result = signals.check_ai_integration(article)

        assert len(result) == 1
        assert result[0]["trigger_data"]["trigger_reason"] == "new_ai_feature"

    def test_returns_empty_no_match(self) -> None:
        """News content without AI features or keywords never fires."""
        assert signals.check_ai_integration(ARTICLE_NEW_FEATURE) == []

    def test_returns_empty_wrong_content_type(self) -> None:
        """The keyword branch only applies to news and opinion content."""
        article = {
            "id": "a",
            "titleEn": "Living with ChatGPT in the car",
            "bodyEn": "A month-long test of the generative AI assistant.",
            "contentType": "review",
            "brandsMentioned": ["Zeekr"],
        }

        assert signals.check_ai_integration(article) == []


class TestCheckOtaDeployment:
    """check_ota_deployment fires on new ota-category features."""

    def test_fires_on_ota_feature(self) -> None:
        """A new ota feature yields a candidate with its name and description."""
        article = {
            "id": "article-ota",
            "contentType": "news",
            "brandsMentioned": ["Zeekr"],
            "featuresExtracted": [
                {
                    "feature_name": "OTA 6.2",
                    "category": "ota",
                    "description": "Quarterly full-vehicle update cadence",
                    "is_new": True,
                }
            ],
        }

        result = signals.check_ota_deployment(article)

        assert len(result) == 1
        assert result[0]["signal_type"] == "ota_deployment"
        assert result[0]["trigger_data"] == {
            "feature_name": "OTA 6.2",
            "description": "Quarterly full-vehicle update cadence",
        }

    def test_returns_empty_no_ota_features(self) -> None:
        """New features in other categories never fire."""
        assert signals.check_ota_deployment(ARTICLE_NEW_FEATURE) == []


class TestCheckPartnershipChange:
    """check_partnership_change fires on multi-brand articles with a signal."""

    def test_fires_on_multi_brand_with_signal(self) -> None:
        """Two brands plus a competitiveSignal yield one candidate."""
        article = {
            "id": "article-partner",
            "brandsMentioned": ["Leapmotor", "Stellantis"],
            "competitiveSignal": "Stellantis deepens its Leapmotor technology tie-up.",
        }

        result = signals.check_partnership_change(article)

        assert len(result) == 1
        assert result[0]["signal_type"] == "partnership_change"
        assert result[0]["features_mentioned"] == []
        assert result[0]["trigger_data"] == {
            "competitive_signal": "Stellantis deepens its Leapmotor technology tie-up.",
            "brand_count": 2,
        }

    def test_returns_empty_single_brand(self) -> None:
        """One brand never fires, even with a competitiveSignal."""
        article = {"id": "a", "brandsMentioned": ["BYD"], "competitiveSignal": "Signal text"}

        assert signals.check_partnership_change(article) == []

    def test_returns_empty_no_competitive_signal(self) -> None:
        """A null competitiveSignal never fires."""
        article = {"id": "a", "brandsMentioned": ["BYD", "NIO"], "competitiveSignal": None}

        assert signals.check_partnership_change(article) == []

    def test_returns_empty_empty_competitive_signal(self) -> None:
        """An empty-string competitiveSignal never fires."""
        article = {"id": "a", "brandsMentioned": ["BYD", "NIO"], "competitiveSignal": ""}

        assert signals.check_partnership_change(article) == []


class TestCheckChipHardware:
    """check_chip_hardware_announcement fires on adas features with chip keywords."""

    def test_fires_on_adas_with_chip_keyword(self) -> None:
        """An adas description mentioning TOPS fires case-insensitively."""
        article = {
            "id": "article-chip",
            "brandsMentioned": ["XPeng"],
            "featuresExtracted": [
                {
                    "feature_name": "Turing platform",
                    "category": "adas",
                    "description": "Delivers 750 TOPS of onboard compute",
                    "is_new": True,
                }
            ],
        }

        result = signals.check_chip_hardware_announcement(article)

        assert len(result) == 1
        assert result[0]["signal_type"] == "chip_hardware_announcement"
        assert "tops" in result[0]["trigger_data"]["matched_keywords"]

    def test_fires_on_nvidia_keyword(self) -> None:
        """A description mentioning Nvidia Orin matches both keywords."""
        article = {
            "id": "article-orin",
            "brandsMentioned": ["Li Auto"],
            "featuresExtracted": [
                {
                    "feature_name": "AD Max",
                    "category": "adas",
                    "description": "Powered by dual Nvidia Orin-X units",
                    "is_new": False,
                }
            ],
        }

        result = signals.check_chip_hardware_announcement(article)

        assert len(result) == 1
        matched = result[0]["trigger_data"]["matched_keywords"]
        assert "nvidia" in matched
        assert "orin" in matched

    def test_returns_empty_no_keyword_match(self) -> None:
        """An adas feature without chip keywords never fires."""
        assert signals.check_chip_hardware_announcement(ARTICLE_NEW_FEATURE) == []

    def test_returns_empty_non_adas_category(self) -> None:
        """Chip keywords outside the adas category never fire."""
        article = {
            "id": "a",
            "brandsMentioned": ["Zeekr"],
            "featuresExtracted": [
                {
                    "feature_name": "Cockpit platform",
                    "category": "infotainment",
                    "description": "Runs on the Snapdragon 8295 chip",
                    "is_new": True,
                }
            ],
        }

        assert signals.check_chip_hardware_announcement(article) == []


class TestDetectSignalCandidates:
    """detect_signal_candidates combines all trigger rules with crash isolation."""

    async def test_runs_all_triggers(self) -> None:
        """An article matching two triggers yields candidates of both types."""
        result = await signals.detect_signal_candidates(ARTICLE_MULTI_SIGNAL, existing_features=[])

        types = {candidate["signal_type"] for candidate in result}
        assert types == {"new_feature_launch", "ai_integration"}

    async def test_skips_trickle_down_when_no_features(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """With existing_features None the trickle-down check is skipped and logged."""
        with caplog.at_level("DEBUG"):
            result = await signals.detect_signal_candidates(ARTICLE_NEW_FEATURE, None)

        assert all(c["signal_type"] != "feature_trickle_down" for c in result)
        assert "skipping trickle-down" in caplog.text

    async def test_single_trigger_failure_doesnt_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One trigger raising still lets the other triggers produce candidates."""

        def _boom(article: dict[str, Any]) -> list[dict[str, Any]]:
            raise RuntimeError("trigger exploded")

        monkeypatch.setattr(signals, "check_new_feature_launch", _boom)

        result = await signals.detect_signal_candidates(ARTICLE_MULTI_SIGNAL, existing_features=[])

        assert [candidate["signal_type"] for candidate in result] == ["ai_integration"]

    async def test_returns_empty_for_irrelevant_article(self) -> None:
        """An article matching no triggers yields no candidates."""
        assert (
            await signals.detect_signal_candidates(ARTICLE_IRRELEVANT, existing_features=[]) == []
        )


class TestGenerateSignalNarrative:
    """generate_signal_narrative calls Sonnet and merges the narrative."""

    @pytest.fixture
    def sonnet(self, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        """Replace anthropic.AsyncAnthropic with a client returning a valid narrative."""
        client = _make_client(json.dumps(SAMPLE_NARRATIVE))
        monkeypatch.setattr(anthropic, "AsyncAnthropic", MagicMock(return_value=client))
        return client

    async def test_returns_narrative_on_success(self, sonnet: MagicMock) -> None:
        """A valid JSON response is merged and the API is called correctly."""
        result = await signals.generate_signal_narrative(SAMPLE_CANDIDATE, ARTICLE_NEW_FEATURE)

        assert result is not None
        assert result["title"] == SAMPLE_NARRATIVE["title"]
        assert result["competitive_impact_score"] == 7
        sonnet.messages.create.assert_awaited_once()
        call_kwargs = sonnet.messages.create.await_args.kwargs
        assert call_kwargs["model"] == settings.SONNET_MODEL
        assert call_kwargs["max_tokens"] == signals.MAX_TOKENS
        content = call_kwargs["messages"][0]["content"]
        assert "new_feature_launch" in content
        assert ARTICLE_NEW_FEATURE["titleEn"] in content

    async def test_returns_none_on_malformed_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-JSON response returns None instead of raising."""
        client = _make_client("Sorry, I cannot produce a narrative for this.")
        monkeypatch.setattr(anthropic, "AsyncAnthropic", MagicMock(return_value=client))

        assert (
            await signals.generate_signal_narrative(SAMPLE_CANDIDATE, ARTICLE_NEW_FEATURE) is None
        )

    async def test_returns_none_on_api_failure(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep: AsyncMock
    ) -> None:
        """A persistently failing API call is retried, then returns None."""
        client = _make_failing_client(anthropic.AnthropicError("server overloaded"))
        monkeypatch.setattr(anthropic, "AsyncAnthropic", MagicMock(return_value=client))

        result = await signals.generate_signal_narrative(SAMPLE_CANDIDATE, ARTICLE_NEW_FEATURE)

        assert result is None
        assert client.messages.create.await_count == settings.MAX_RETRIES + 1

    async def test_merges_narrative_into_candidate(self, sonnet: MagicMock) -> None:
        """The result carries both candidate fields and narrative fields."""
        result = await signals.generate_signal_narrative(SAMPLE_CANDIDATE, ARTICLE_NEW_FEATURE)

        assert result is not None
        assert result["signal_type"] == "new_feature_launch"
        assert result["source_article_ids"] == ["article-launch"]
        assert result["trigger_data"] == SAMPLE_CANDIDATE["trigger_data"]
        assert result["summary"] == SAMPLE_NARRATIVE["summary"]
        assert (
            result["implications_for_western_oems"]
            == (SAMPLE_NARRATIVE["implications_for_western_oems"])
        )


class TestDetectSignalsFromArticles:
    """detect_signals_from_articles orchestrates the full two-stage batch."""

    @pytest.fixture
    def flow_mocks(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
        """Mock the Firestore helpers and Stage 2 narrative generation."""
        mocks = {
            "get_all_features": AsyncMock(return_value=[]),
            "save_signal": AsyncMock(return_value="signal-1"),
            "generate_signal_narrative": AsyncMock(
                side_effect=lambda candidate, article: {**candidate, **SAMPLE_NARRATIVE}
            ),
        }
        for name, mock in mocks.items():
            monkeypatch.setattr(signals, name, mock)
        return mocks

    async def test_full_flow(self, flow_mocks: dict[str, AsyncMock]) -> None:
        """One triggering and one irrelevant article produce one saved signal."""
        summary = await signals.detect_signals_from_articles(
            [ARTICLE_NEW_FEATURE, ARTICLE_IRRELEVANT]
        )

        assert summary["articles_processed"] == 2
        assert summary["articles_skipped_duplicate"] == 0
        assert summary["candidates_detected"] == 1
        assert summary["signals_generated"] == 1
        assert summary["signals_failed"] == 0
        assert summary["signals_by_type"]["new_feature_launch"] == 1
        flow_mocks["save_signal"].assert_awaited_once()
        flow_mocks["get_all_features"].assert_awaited_once()

    async def test_skips_duplicate_articles(self, flow_mocks: dict[str, AsyncMock]) -> None:
        """An isDuplicate article is counted as skipped and never analyzed."""
        summary = await signals.detect_signals_from_articles([ARTICLE_DUPLICATE])

        assert summary["articles_skipped_duplicate"] == 1
        assert summary["articles_processed"] == 0
        assert summary["signals_generated"] == 0
        flow_mocks["generate_signal_narrative"].assert_not_awaited()

    async def test_multiple_signals_from_one_article(
        self, flow_mocks: dict[str, AsyncMock]
    ) -> None:
        """An article triggering two types generates two signals."""
        summary = await signals.detect_signals_from_articles([ARTICLE_MULTI_SIGNAL])

        assert summary["candidates_detected"] == 2
        assert summary["signals_generated"] == 2
        assert summary["signals_by_type"]["new_feature_launch"] == 1
        assert summary["signals_by_type"]["ai_integration"] == 1
        assert flow_mocks["save_signal"].await_count == 2

    async def test_no_signals_returns_zero_counts(self, flow_mocks: dict[str, AsyncMock]) -> None:
        """A batch with no triggering articles returns zeroed counts."""
        summary = await signals.detect_signals_from_articles([ARTICLE_IRRELEVANT])

        assert summary["articles_processed"] == 1
        assert summary["candidates_detected"] == 0
        assert summary["signals_generated"] == 0
        assert summary["signals_failed"] == 0
        assert all(count == 0 for count in summary["signals_by_type"].values())
        flow_mocks["save_signal"].assert_not_awaited()

    async def test_single_article_failure_doesnt_crash(
        self, monkeypatch: pytest.MonkeyPatch, flow_mocks: dict[str, AsyncMock]
    ) -> None:
        """One article erroring in Stage 1 does not abort the remaining articles."""
        monkeypatch.setattr(
            signals,
            "detect_signal_candidates",
            AsyncMock(side_effect=[RuntimeError("stage 1 exploded"), [dict(SAMPLE_CANDIDATE)]]),
        )

        summary = await signals.detect_signals_from_articles(
            [ARTICLE_NEW_FEATURE, ARTICLE_AI_KEYWORD]
        )

        assert summary["articles_processed"] == 2
        assert summary["candidates_detected"] == 1
        assert summary["signals_generated"] == 1

    async def test_llm_failure_counted_in_signals_failed(
        self, flow_mocks: dict[str, AsyncMock]
    ) -> None:
        """A failed narrative is counted and never saved."""
        flow_mocks["generate_signal_narrative"].side_effect = None
        flow_mocks["generate_signal_narrative"].return_value = None

        summary = await signals.detect_signals_from_articles([ARTICLE_NEW_FEATURE])

        assert summary["candidates_detected"] == 1
        assert summary["signals_generated"] == 0
        assert summary["signals_failed"] == 1
        flow_mocks["save_signal"].assert_not_awaited()

    async def test_saves_signal_to_firestore(self, flow_mocks: dict[str, AsyncMock]) -> None:
        """save_signal receives the schema fields and no trigger_data."""
        await signals.detect_signals_from_articles([ARTICLE_NEW_FEATURE])

        flow_mocks["save_signal"].assert_awaited_once()
        assert flow_mocks["save_signal"].await_args is not None
        (data,) = flow_mocks["save_signal"].await_args.args
        assert data == {
            "signal_type": "new_feature_launch",
            "title": SAMPLE_NARRATIVE["title"],
            "summary": SAMPLE_NARRATIVE["summary"],
            "brands_mentioned": ["BYD"],
            "features_mentioned": ["City NOA"],
            "source_article_ids": ["article-launch"],
            "implications_for_western_oems": SAMPLE_NARRATIVE["implications_for_western_oems"],
            "competitive_impact_score": 7,
        }
        assert "trigger_data" not in data
