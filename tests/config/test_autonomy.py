# test-path: cross-cutting
"""The single per-overlay ``autonomy`` switch (souliane/teatree#1668).

One coherent value — three tiers ``full > notify > babysit`` (default
``babysit``) — that governs the whole USER-in-the-loop approval surface for an
overlay. Under ``full`` OR ``notify`` the three scattered approval gates
(``on_behalf_post_mode``, ``require_human_approval_to_merge``,
``require_human_approval_to_answer``) collapse to their autonomous value in
``get_effective_settings`` and ``mode`` is pinned to ``auto``, UNLESS the user
pinned an explicit per-gate override (explicit always wins — autonomy never
silently overrides an opinion). ``notify`` additionally derives
``notify_on_behalf = True``; ``full`` and ``babysit`` keep it ``False``.

Under the #1775 DB partition, ``autonomy`` / ``mode`` / the three gates are all
DB-home, so this exercises the collapse via ``ConfigSetting`` rows: an
overlay-scoped row is the per-overlay opinion (``hard_pinned``); a global-scope
row is the global opinion (still wins for a gate, harmless for ``mode``).

The safety/quality floor is out of scope by construction. ``autoload`` is untouched
by the collapse; ``orchestrator_bash_gate_enabled`` keeps its never-lockout default
and is never relaxed.
"""

import pytest
from django.test import TestCase

from teatree.config import Autonomy, Mode, OnBehalfPostMode, get_effective_settings
from teatree.core.models import ConfigSetting


class TestAutonomyParse:
    def test_parse_full(self) -> None:
        assert Autonomy.parse("full") is Autonomy.FULL

    def test_parse_notify(self) -> None:
        assert Autonomy.parse("notify") is Autonomy.NOTIFY

    def test_parse_babysit(self) -> None:
        assert Autonomy.parse("babysit") is Autonomy.BABYSIT

    def test_parse_is_case_insensitive(self) -> None:
        assert Autonomy.parse("  FULL ") is Autonomy.FULL

    def test_parse_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid autonomy"):
            Autonomy.parse("yolo")

    def test_tier_ordering_full_gt_notify_gt_babysit(self) -> None:
        """Documented tier ordering: full > notify > babysit (default babysit)."""
        members = list(Autonomy)
        assert members == [Autonomy.BABYSIT, Autonomy.NOTIFY, Autonomy.FULL]


class TestAutonomyDefault(TestCase):
    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_MODE", "T3_ON_BEHALF_POST_MODE"):
            monkeypatch.delenv(env, raising=False)

    def test_defaults_to_full(self) -> None:
        assert get_effective_settings().autonomy is Autonomy.FULL

    def test_full_default_collapses_the_gate_values(self) -> None:
        settings = get_effective_settings()
        assert settings.on_behalf_post_mode is OnBehalfPostMode.IMMEDIATE
        assert settings.require_human_approval_to_merge is False
        assert settings.require_human_approval_to_answer is False


class _AutonomyDbBase(TestCase):
    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_MODE", "T3_ON_BEHALF_POST_MODE"):
            monkeypatch.delenv(env, raising=False)
        self.monkeypatch = monkeypatch


class TestAutonomyFullResolution(_AutonomyDbBase):
    def test_per_overlay_full_flips_all_three_gates(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "full", scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        settings = get_effective_settings()
        assert settings.autonomy is Autonomy.FULL
        assert settings.on_behalf_post_mode is OnBehalfPostMode.IMMEDIATE
        assert settings.require_human_approval_to_merge is False
        assert settings.require_human_approval_to_answer is False

    def test_full_leaves_safety_floor_untouched(self) -> None:
        # ``autoload`` / ``orchestrator_bash_gate_enabled`` are untouched by the
        # autonomy collapse — the safety floor is never relaxed.
        ConfigSetting.objects.set_value("autoload", value=True, scope="")
        ConfigSetting.objects.set_value("autonomy", "full", scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        settings = get_effective_settings()
        assert settings.autoload is True
        assert settings.orchestrator_bash_gate_enabled is True

    def test_explicit_per_gate_override_wins_over_full(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "full", scope="trusted")
        ConfigSetting.objects.set_value("require_human_approval_to_merge", value=True, scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        settings = get_effective_settings()
        assert settings.on_behalf_post_mode is OnBehalfPostMode.IMMEDIATE
        assert settings.require_human_approval_to_answer is False
        assert settings.require_human_approval_to_merge is True

    def test_babysit_overlay_keeps_gates_blocking(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "babysit", scope="careful")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "careful")
        settings = get_effective_settings()
        assert settings.autonomy is Autonomy.BABYSIT
        assert settings.on_behalf_post_mode is OnBehalfPostMode.DRAFT_OR_ASK
        assert settings.require_human_approval_to_merge is True
        assert settings.require_human_approval_to_answer is True

    def test_one_overlay_full_does_not_leak_to_another(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "full", scope="trusted")
        ConfigSetting.objects.set_value("autonomy", "babysit", scope="careful")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "careful")
        careful = get_effective_settings()
        assert careful.require_human_approval_to_merge is True
        assert careful.on_behalf_post_mode is OnBehalfPostMode.DRAFT_OR_ASK
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        trusted = get_effective_settings()
        assert trusted.require_human_approval_to_merge is False
        assert trusted.on_behalf_post_mode is OnBehalfPostMode.IMMEDIATE

    def test_full_keeps_mode_auto_consistent(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "full", scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        assert get_effective_settings().mode is Mode.AUTO

    def test_full_keeps_notify_on_behalf_false(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "full", scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        assert get_effective_settings().notify_on_behalf is False

    def test_babysit_keeps_notify_on_behalf_false(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "babysit", scope="careful")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "careful")
        assert get_effective_settings().notify_on_behalf is False


class TestAutonomyNotifyTier(_AutonomyDbBase):
    def test_notify_flips_the_same_three_gates_as_full(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "notify", scope="client")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "client")
        settings = get_effective_settings()
        assert settings.autonomy is Autonomy.NOTIFY
        assert settings.on_behalf_post_mode is OnBehalfPostMode.IMMEDIATE
        assert settings.require_human_approval_to_merge is False
        assert settings.require_human_approval_to_answer is False
        assert settings.mode is Mode.AUTO

    def test_notify_derives_notify_on_behalf_true(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "notify", scope="client")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "client")
        assert get_effective_settings().notify_on_behalf is True

    def test_notify_leaves_safety_floor_untouched(self) -> None:
        # ``autoload`` / ``orchestrator_bash_gate_enabled`` survive the notify collapse.
        ConfigSetting.objects.set_value("autoload", value=True, scope="")
        ConfigSetting.objects.set_value("autonomy", "notify", scope="client")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "client")
        settings = get_effective_settings()
        assert settings.autoload is True
        assert settings.orchestrator_bash_gate_enabled is True

    def test_notify_isolated_from_full_overlay(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "full", scope="t3-teatree")
        ConfigSetting.objects.set_value("autonomy", "notify", scope="t3-client")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "t3-teatree")
        teatree = get_effective_settings()
        assert teatree.autonomy is Autonomy.FULL
        assert teatree.notify_on_behalf is False
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "t3-client")
        client = get_effective_settings()
        assert client.autonomy is Autonomy.NOTIFY
        assert client.notify_on_behalf is True
        assert client.require_human_approval_to_merge is False


class TestAutonomyReviewRequestPostDisabled(_AutonomyDbBase):
    """The resolved ``review_request_post_disabled`` bool is set per autonomy tier.

    The parallel side flag ``agent_review_request_disabled`` is deleted; the
    collapse now drives review-request blocking off the tier (Option A — a
    per-overlay explicit pin still escapes):

    * ``notify`` → True  (collaborative/customer surface: BLOCK review-request),
    * ``full``   → False (solo tooling surface: PROCEED),
    * ``babysit``→ default False (review-request follows ``on_behalf_post_mode``).
    """

    def test_notify_resolves_review_request_post_disabled_true(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "notify", scope="client")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "client")
        assert get_effective_settings().review_request_post_disabled is True

    def test_full_resolves_review_request_post_disabled_false(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "full", scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        assert get_effective_settings().review_request_post_disabled is False

    def test_babysit_keeps_review_request_post_disabled_default_false(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "babysit", scope="careful")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "careful")
        assert get_effective_settings().review_request_post_disabled is False

    def test_explicit_pin_wins_over_full_tier(self) -> None:
        # Option A: an explicit per-overlay pin of the resolved field beats the
        # ``full`` tier's PROCEED default.
        ConfigSetting.objects.set_value("autonomy", "full", scope="trusted")
        ConfigSetting.objects.set_value("review_request_post_disabled", value=True, scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        assert get_effective_settings().review_request_post_disabled is True

    def test_explicit_pin_wins_over_notify_tier(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "notify", scope="client")
        ConfigSetting.objects.set_value("review_request_post_disabled", value=False, scope="client")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "client")
        assert get_effective_settings().review_request_post_disabled is False

    def test_notify_does_not_leak_disabled_to_full_overlay(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "notify", scope="t3-client")
        ConfigSetting.objects.set_value("autonomy", "full", scope="t3-teatree")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "t3-teatree")
        assert get_effective_settings().review_request_post_disabled is False
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "t3-client")
        assert get_effective_settings().review_request_post_disabled is True


class TestAutonomyOverPinFix(_AutonomyDbBase):
    """A global ``mode`` must NOT defeat the autonomy ``mode = auto`` pin (#1668)."""

    def test_global_interactive_mode_does_not_defeat_full_mode_auto(self) -> None:
        ConfigSetting.objects.set_value("mode", "interactive")  # global
        ConfigSetting.objects.set_value("autonomy", "full", scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        settings = get_effective_settings()
        # A global ``mode = interactive`` is a workspace default — the collapse wins.
        assert settings.mode is Mode.AUTO
        assert settings.require_human_approval_to_merge is False

    def test_global_interactive_mode_does_not_defeat_notify_mode_auto(self) -> None:
        ConfigSetting.objects.set_value("mode", "interactive")  # global
        ConfigSetting.objects.set_value("autonomy", "notify", scope="client")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "client")
        assert get_effective_settings().mode is Mode.AUTO

    def test_per_overlay_explicit_mode_still_wins(self) -> None:
        ConfigSetting.objects.set_value("mode", "interactive", scope="trusted")
        ConfigSetting.objects.set_value("autonomy", "full", scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        settings = get_effective_settings()
        # A per-overlay ``mode`` is a deliberate opinion — autonomy must not override it.
        assert settings.mode is Mode.INTERACTIVE
        assert settings.require_human_approval_to_merge is False

    def test_global_explicit_gate_still_wins_over_collapse(self) -> None:
        ConfigSetting.objects.set_value("require_human_approval_to_merge", value=True)  # global
        ConfigSetting.objects.set_value("autonomy", "full", scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        assert get_effective_settings().require_human_approval_to_merge is True
