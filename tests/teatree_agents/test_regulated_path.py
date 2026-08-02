"""The regulated-path eligibility gate — the EU data-residency / compliance allowlist (#2887).

Extracted out of ``teatree.agents.model_tiering`` (which now owns only tier→model
resolution) into ``teatree.agents.regulated_path``. These pins mirror that move.
"""

import pytest
from django.test import TestCase

from teatree.agents.regulated_path import (
    RegulatedPathPolicy,
    assert_model_allowed_on_regulated_path,
    is_regulated_path_eligible,
)
from teatree.config import UserSettings
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


class TestRegulatedPathPolicy(TestCase):
    """The policy carried as a VALUE (#3980) — resolved once, applied where the read is illegal."""

    def test_the_shipped_default_enforces_nothing(self) -> None:
        RegulatedPathPolicy().assert_allowed("deepseek/deepseek-v4-pro")

    def test_it_resolves_from_the_stored_settings(self) -> None:
        ConfigSetting.objects.set_value("enforce_regulated_path", value=True)
        ConfigSetting.objects.set_value("regulated_path_model_allowlist", value=["anthropic/"])
        assert RegulatedPathPolicy.from_settings() == RegulatedPathPolicy(enforce=True, allowlist=("anthropic/",))

    def test_supplied_settings_win_over_a_fresh_resolution(self) -> None:
        # The dispatch path passes the settings it ALREADY resolved for the task's overlay scope,
        # so the transport and the gate can never read two different scopes.
        supplied = UserSettings(enforce_regulated_path=True, regulated_path_model_allowlist=["google/"])
        assert RegulatedPathPolicy.from_settings(supplied) == RegulatedPathPolicy(enforce=True, allowlist=("google/",))

    def test_a_resolved_policy_still_refuses_an_ineligible_model(self) -> None:
        policy = RegulatedPathPolicy(enforce=True, allowlist=("anthropic/",))
        with pytest.raises(ValueError, match="not eligible for the regulated path"):
            policy.assert_allowed("deepseek/deepseek-v4-pro")

    def test_resolve_keeps_a_supplied_policy(self) -> None:
        supplied = RegulatedPathPolicy(enforce=True, allowlist=("google/",))
        assert RegulatedPathPolicy.resolve(supplied) is supplied

    def test_resolve_falls_back_to_the_stored_settings(self) -> None:
        # Without this fallback an unresolved policy would silently STOP enforcing — a
        # compliance gate narrowed by a construction detail.
        ConfigSetting.objects.set_value("enforce_regulated_path", value=True)
        ConfigSetting.objects.set_value("regulated_path_model_allowlist", value=["anthropic/"])
        with pytest.raises(ValueError, match="not eligible for the regulated path"):
            RegulatedPathPolicy.resolve(None).assert_allowed("deepseek/deepseek-v4-pro")
