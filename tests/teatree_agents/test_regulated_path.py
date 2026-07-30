"""The regulated-path eligibility gate — the EU data-residency / compliance allowlist (#2887).

Extracted out of ``teatree.agents.model_tiering`` (which now owns only tier→model
resolution) into ``teatree.agents.regulated_path``. These pins mirror that move.
"""

import pytest
from django.test import TestCase

from teatree.agents.regulated_path import assert_model_allowed_on_regulated_path, is_regulated_path_eligible
from teatree.core.models import ConfigSetting


class TestIsRegulatedPathEligible:
    """:func:`is_regulated_path_eligible` — membership in the explicit allowlist, case-insensitively."""

    @pytest.mark.parametrize("model_id", ["anthropic/claude-opus-4.8", "Anthropic/Claude-Sonnet", "google/gemini-3"])
    def test_an_allowlisted_pattern_is_eligible(self, model_id: str) -> None:
        assert is_regulated_path_eligible(model_id, ["anthropic/", "google/"])

    @pytest.mark.parametrize("model_id", ["deepseek/deepseek-v4-pro", "qwen/qwen3.6-plus"])
    def test_a_model_off_the_allowlist_is_ineligible(self, model_id: str) -> None:
        assert not is_regulated_path_eligible(model_id, ["anthropic/", "google/"])

    def test_empty_allowlist_makes_nothing_eligible(self) -> None:
        assert not is_regulated_path_eligible("anthropic/claude-opus-4.8", [])


class TestAssertModelAllowedOnRegulatedPath:
    """:func:`assert_model_allowed_on_regulated_path` — the regulated-lane allowlist gate."""

    def test_unenforced_lane_never_raises(self) -> None:
        # The teatree factory lane carries no regulated data — any model runs.
        assert_model_allowed_on_regulated_path("deepseek/deepseek-v4-pro", enforce_regulated_path=False, allowlist=[])

    def test_allowlisted_model_on_the_regulated_path_is_a_noop(self) -> None:
        assert_model_allowed_on_regulated_path(
            "anthropic/claude-opus-4.8", enforce_regulated_path=True, allowlist=["anthropic/"]
        )

    def test_model_off_the_allowlist_is_refused_on_the_regulated_path(self) -> None:
        with pytest.raises(ValueError, match="not eligible for the regulated path"):
            assert_model_allowed_on_regulated_path(
                "deepseek/deepseek-v4-pro", enforce_regulated_path=True, allowlist=["anthropic/"]
            )

    def test_enforced_but_empty_allowlist_refuses_everything(self) -> None:
        with pytest.raises(ValueError, match="not eligible for the regulated path"):
            assert_model_allowed_on_regulated_path(
                "anthropic/claude-opus-4.8", enforce_regulated_path=True, allowlist=[]
            )


class TestAssertModelAllowedDefaultSettings(TestCase):
    """The default (params ``None``) reads the resolved DB-home regulated-path settings."""

    def test_default_unenforced_never_raises(self) -> None:
        # No row set — enforce_regulated_path defaults False, so nothing is gated.
        assert_model_allowed_on_regulated_path("deepseek/deepseek-v4-pro")

    def test_default_reads_the_resolved_regulated_path_settings(self) -> None:
        ConfigSetting.objects.set_value("enforce_regulated_path", value=True)
        ConfigSetting.objects.set_value("regulated_path_model_allowlist", value=["anthropic/"])
        with pytest.raises(ValueError, match="not eligible for the regulated path"):
            assert_model_allowed_on_regulated_path("deepseek/deepseek-v4-pro")

    def test_allowlisted_model_passes_under_enforcement(self) -> None:
        ConfigSetting.objects.set_value("enforce_regulated_path", value=True)
        ConfigSetting.objects.set_value("regulated_path_model_allowlist", value=["anthropic/", "claude"])
        assert_model_allowed_on_regulated_path("anthropic/claude-opus-4.8")
