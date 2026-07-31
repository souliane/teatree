# test-path: cross-cutting
"""The single per-overlay ``autonomy`` switch (souliane/teatree#1668).

One coherent value — three tiers ``full > notify > babysit`` (shipped ``full``) —
that governs the whole USER-in-the-loop approval surface for an overlay. Under
``full`` OR ``notify`` the collapsed approval gate
(``require_human_approval_to_answer``) takes its autonomous value in
``get_effective_settings`` and ``mode`` is pinned to ``auto``, UNLESS the user
pinned an explicit per-gate override (explicit always wins — autonomy never
silently overrides an opinion). ``notify`` additionally derives
``notify_on_behalf = True``; ``full`` and ``babysit`` keep it ``False``.

Two gates are deliberately NOT in that set, for the same reason: how far the agent
carries work on its own is a separate concern from surrendering a distinct human
control, and no tier may remove one as a side effect. Each is its own named opt-in
— ``require_human_approval_to_merge = false`` for review before merge (#3630), and
``on_behalf_post_mode = "immediate"`` for speaking to a colleague under the owner's
identity (#3895).

Under the #1775 DB partition, ``autonomy`` / ``mode`` / the collapsed gate / both
excluded gates are all DB-home, so this exercises the collapse via ``ConfigSetting`` rows: an
overlay-scoped row is the per-overlay opinion (``hard_pinned``); a global-scope
row is the global opinion (still wins for a gate, harmless for ``mode``).

The safety/quality floor is out of scope by construction. ``autoload`` is untouched
by the collapse; ``orchestrator_bash_gate_enabled`` keeps its never-lockout default
and is never relaxed.
"""

import pytest
from django.test import TestCase

from teatree.config import SAFETY_POSTURE_KEYS, Autonomy, Mode, OnBehalfPostMode, get_effective_settings
from teatree.config.resolution import _AUTONOMY_COLLAPSED_GATE_VALUES, AUTONOMY_COLLAPSED_FIELDS
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
        """Documented tier ordering: full > notify > babysit (default full)."""
        members = list(Autonomy)
        assert members == [Autonomy.BABYSIT, Autonomy.NOTIFY, Autonomy.FULL]


class TestCollapseSetMembership:
    """Pin the exclusions structurally, so no edit can re-enrol a gate the tier must not write.

    The per-tier assertions elsewhere in this module observe the RESOLVED value; these
    observe the set itself, which is what a future edit would actually touch.
    """

    def test_review_before_merge_is_not_a_collapsed_gate(self) -> None:
        assert "require_human_approval_to_merge" not in _AUTONOMY_COLLAPSED_GATE_VALUES

    def test_colleague_egress_is_not_a_collapsed_gate(self) -> None:
        assert "on_behalf_post_mode" not in _AUTONOMY_COLLAPSED_GATE_VALUES

    def test_no_safety_posture_key_is_tier_derived(self) -> None:
        """The generalization of both exclusions, covering the fields derived outside the set too.

        ``config_setting_set`` refuses a ``SAFETY_POSTURE_KEYS`` write by declared effect so an
        agent can never self-grant one. A tier that DERIVED such a key would grant it by the back
        door, since the resolved value needs no row an audit could read.
        """
        assert not SAFETY_POSTURE_KEYS & AUTONOMY_COLLAPSED_FIELDS


class TestAutonomyDefault(TestCase):
    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_MODE", "T3_ON_BEHALF_POST_MODE"):
            monkeypatch.delenv(env, raising=False)

    def test_defaults_to_full(self) -> None:
        assert get_effective_settings().autonomy is Autonomy.FULL

    def test_full_default_collapses_the_answering_gate(self) -> None:
        assert get_effective_settings().require_human_approval_to_answer is False

    def test_the_default_tier_still_does_not_remove_review_before_merge(self) -> None:
        # #3630: the merge gate is not a tier-collapsed gate, so raising the SHIPPED
        # default to ``full`` cannot silently drop review-before-merge either. Merging
        # unreviewed stays its own named opt-in.
        assert get_effective_settings().require_human_approval_to_merge is True

    def test_the_default_tier_still_does_not_open_colleague_egress(self) -> None:
        # #3895, the same argument one gate over: raising the SHIPPED default to
        # ``full`` cannot silently hand the agent the owner's colleague-facing voice.
        assert get_effective_settings().on_behalf_post_mode is OnBehalfPostMode.DRAFT_OR_ASK


class _AutonomyDbBase(TestCase):
    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_MODE", "T3_ON_BEHALF_POST_MODE"):
            monkeypatch.delenv(env, raising=False)
        self.monkeypatch = monkeypatch


class TestAutonomyFullResolution(_AutonomyDbBase):
    def test_per_overlay_full_flips_the_collapsed_gate(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "full", scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        settings = get_effective_settings()
        assert settings.autonomy is Autonomy.FULL
        assert settings.require_human_approval_to_answer is False

    def test_full_never_removes_the_merge_review_gate(self) -> None:
        """#3630 — the highest tier must not silently disable review before merge."""
        ConfigSetting.objects.set_value("autonomy", "full", scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        assert get_effective_settings().require_human_approval_to_merge is True

    def test_full_never_opens_colleague_egress(self) -> None:
        """#3895 — the highest tier must not silently hand over the owner's voice."""
        ConfigSetting.objects.set_value("autonomy", "full", scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        assert get_effective_settings().on_behalf_post_mode is OnBehalfPostMode.DRAFT_OR_ASK

    def test_merge_without_review_is_its_own_explicit_opt_in(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "full", scope="trusted")
        ConfigSetting.objects.set_value("require_human_approval_to_merge", value=False, scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        assert get_effective_settings().require_human_approval_to_merge is False

    def test_opening_colleague_egress_is_its_own_explicit_opt_in(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "full", scope="trusted")
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate", scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        assert get_effective_settings().on_behalf_post_mode is OnBehalfPostMode.IMMEDIATE

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
        ConfigSetting.objects.set_value("require_human_approval_to_answer", value=True, scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        settings = get_effective_settings()
        assert settings.require_human_approval_to_answer is True
        # The tier still resolved — the pin beat the collapse, it did not skip it.
        assert settings.mode is Mode.AUTO

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
        assert careful.require_human_approval_to_answer is True
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        trusted = get_effective_settings()
        assert trusted.require_human_approval_to_answer is False

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
    def test_notify_flips_the_same_gate_as_full(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "notify", scope="client")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "client")
        settings = get_effective_settings()
        assert settings.autonomy is Autonomy.NOTIFY
        assert settings.require_human_approval_to_answer is False
        assert settings.mode is Mode.AUTO

    def test_notify_never_removes_the_merge_review_gate(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "notify", scope="client")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "client")
        assert get_effective_settings().require_human_approval_to_merge is True

    def test_notify_never_opens_colleague_egress(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "notify", scope="client")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "client")
        assert get_effective_settings().on_behalf_post_mode is OnBehalfPostMode.DRAFT_OR_ASK

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

    def test_global_explicit_gate_still_wins_over_collapse(self) -> None:
        ConfigSetting.objects.set_value("require_human_approval_to_answer", value=True)  # global
        ConfigSetting.objects.set_value("autonomy", "full", scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        assert get_effective_settings().require_human_approval_to_answer is True
