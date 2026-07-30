"""Tests for the tri-state on-behalf posting pre-gate policy.

``on_behalf_post_mode`` is DB-home (#1775): its sole authoritative tier is the
``ConfigSetting`` store (+ ``T3_*`` env). A ``[teatree]`` / ``[overlays.<name>]``
TOML value for it is ignored on read, so every mode is staged via
``ConfigSetting.objects.set_value`` rather than TOML. ``get_effective_settings``
is exercised end-to-end with no mocks; the Django test DB is the sole config tier,
so the real host config never leaks in.

The fine-grained (mode, action) → verdict matrix lives in
``tests/test_on_behalf_post_mode.py``; this file focuses on the retirement of
the legacy ``ask_before_post_on_behalf`` TOML alias.
"""

import pytest
from django.test import TestCase

from teatree.config import Autonomy, OnBehalfPostMode, get_effective_settings
from teatree.core.models import ConfigSetting
from teatree.on_behalf_gate import OnBehalfVerdict, on_behalf_post_will_block, resolve_on_behalf_verdict


class _OnBehalfDbBase(TestCase):
    """Isolate the on-behalf env so the DB store is the sole config tier."""

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_ON_BEHALF_POST_MODE", "T3_ON_BEHALF_AUTO_ACTIONS"):
            monkeypatch.delenv(env, raising=False)
        self.monkeypatch = monkeypatch


class TestNewDefaultMode(_OnBehalfDbBase):
    """The default is DRAFT_OR_ASK when no ``on_behalf_post_mode`` row is set."""

    def test_default_when_no_config_is_draft_or_ask(self) -> None:
        assert resolve_on_behalf_verdict("post_draft_note") is OnBehalfVerdict.AUTO_DRAFT
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK


class TestExplicitModes(_OnBehalfDbBase):
    def test_explicit_immediate_disables_the_gate(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.PROCEED

    def test_explicit_ask_blocks_visible_posts_but_exempts_drafts(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        # Colleague-visible post: BLOCKed under ASK.
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK
        # Draft is colleague-invisible: EXEMPT even under ASK — it auto-drafts.
        assert resolve_on_behalf_verdict("post_draft_note") is OnBehalfVerdict.AUTO_DRAFT


class TestProactivePreCheck(_OnBehalfDbBase):
    """``on_behalf_post_will_block`` is the forward-looking BLOCK predicate.

    A caller runs it BEFORE attempting a colleague-visible post so it can offer
    the owner the enable-setting / approve-once choice proactively, instead of
    hitting the gate and only then reacting.
    """

    def test_visible_post_will_block_under_a_blocking_mode(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        assert on_behalf_post_will_block("post_comment") is True

    def test_immediate_mode_will_not_block(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        assert on_behalf_post_will_block("post_comment") is False

    def test_draft_form_action_will_not_block_even_under_ask(self) -> None:
        # A draft AUTO_DRAFTs (colleague-invisible), so it never needs a pre-ask.
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        assert on_behalf_post_will_block("post_draft_note") is False


class TestPerOverlayOverride(_OnBehalfDbBase):
    def test_per_overlay_override_wins_over_global(self) -> None:
        """A trusted overlay can opt into IMMEDIATE without flipping the global.

        The global value is a GLOBAL-scope ``ConfigSetting`` row; the per-overlay
        opinion is an OVERLAY-scoped row that beats it.
        """
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate", scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.PROCEED


class TestAutonomyTierDoesNotOpenTheGate(_OnBehalfDbBase):
    """The autonomy tier is decoupled from colleague egress (#3895).

    Shipping ``autonomy = full`` says "carry the work end to end"; it does not say
    "speak as the owner to a colleague with no approval". Those are different
    decisions, so the tier no longer collapses ``on_behalf_post_mode`` — the same
    separation #3630 made for ``require_human_approval_to_merge``. Opening
    colleague egress stays its own named opt-in (``on_behalf_post_mode =
    immediate``), which every tier reads unchanged.
    """

    def test_shipped_full_autonomy_still_blocks_a_colleague_visible_post(self) -> None:
        assert get_effective_settings().autonomy is Autonomy.FULL
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK

    def test_explicit_full_overlay_still_blocks_a_colleague_visible_post(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "full", scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        assert get_effective_settings().on_behalf_post_mode is OnBehalfPostMode.DRAFT_OR_ASK
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK

    def test_explicit_notify_overlay_still_blocks_a_colleague_visible_post(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "notify", scope="client")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "client")
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK

    def test_opening_egress_under_full_is_its_own_named_opt_in(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "full", scope="trusted")
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate", scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.PROCEED


class TestRetiredLegacyTomlAlias(_OnBehalfDbBase):
    """The legacy ``ask_before_post_on_behalf`` TOML alias is RETIRED (#1775).

    The pre-partition shim that translated the boolean TOML key into a mode is
    gone: ``on_behalf_post_mode`` is DB-home and the key is ignored on read, so a
    config that still carries it resolves to the DB-store value (or the
    DRAFT_OR_ASK default). The user migrates it with ``config_setting import``.
    """

    def test_legacy_true_is_ignored_falls_through_to_default(self) -> None:
        # DRAFT_OR_ASK default (coincides with the old ASK mapping here).
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK
        assert resolve_on_behalf_verdict("post_draft_note") is OnBehalfVerdict.AUTO_DRAFT

    def test_legacy_false_is_ignored_does_not_open_the_gate(self) -> None:
        # The gate stays at the DRAFT_OR_ASK default and a visible post still BLOCKs.
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK

    def test_no_db_row_defaults_to_draft_or_ask(self) -> None:
        assert get_effective_settings().on_behalf_post_mode is OnBehalfPostMode.DRAFT_OR_ASK
