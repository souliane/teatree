"""Resolver tests for ``resolve_on_behalf_verdict(action)``.

The matrix of (mode, action) → verdict exercises :mod:`teatree.config` +
:mod:`teatree.on_behalf_gate` end-to-end. ``on_behalf_post_mode`` and
``on_behalf_auto_actions`` are DB-home (#1775): their sole authoritative tier is
the ``ConfigSetting`` store (+ ``T3_*`` env). A ``[teatree]`` /
``[overlays.<name>]`` TOML value for either is ignored on read, so every mode /
allowlist is staged via ``ConfigSetting.objects.set_value`` rather than TOML. The
Django test DB is the sole config tier, so the real host config never leaks in.
Mirrors :mod:`tests.test_on_behalf_gate`.
"""

import pytest
from django.test import TestCase

from teatree.config import OnBehalfPostMode
from teatree.core.models import ConfigSetting
from teatree.on_behalf_gate import OnBehalfVerdict, resolve_on_behalf_verdict


class _OnBehalfDbBase(TestCase):
    """Isolate the on-behalf env so the DB store is the sole config tier."""

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_ON_BEHALF_POST_MODE", "T3_ON_BEHALF_AUTO_ACTIONS"):
            monkeypatch.delenv(env, raising=False)
        self.monkeypatch = monkeypatch


# Every gated colleague-facing action we ship, kept in one list so a
# future addition (e.g. a new ``post_reaction``) only needs the
# parametrisation to be extended, not the test bodies.
_NON_DRAFT_ACTIONS = [
    "post_comment",
    "publish_draft_notes",
    "reply_to_discussion",
    "resolve_discussion",
    "update_note",
    "approve",
    "post_evidence",
    "post_in_thread",
]
_DRAFT_FORM_ACTIONS = ["post_draft_note"]
# Actions that resolve to PROCEED even under a blocking mode because they are
# the user's routine self-documentation on their OWN ticket (default allowlist).
_AUTO_ACTIONS = ["post_e2e_evidence"]


class TestImmediateMode(_OnBehalfDbBase):
    def test_immediate_always_passes(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        for action in (*_DRAFT_FORM_ACTIONS, *_NON_DRAFT_ACTIONS):
            with self.subTest(action=action):
                assert resolve_on_behalf_verdict(action) is OnBehalfVerdict.PROCEED


class TestAskMode(_OnBehalfDbBase):
    """ASK blocks colleague-VISIBLE posts but EXEMPTS drafts (#draft-bypass).

    A draft is colleague-invisible, so it never needs approval — even
    under strict ASK it resolves to AUTO_DRAFT, identical to DRAFT_OR_ASK.
    Only colleague-visible actions BLOCK.
    """

    def test_ask_exempts_draft_form_actions(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        for action in _DRAFT_FORM_ACTIONS:
            with self.subTest(action=action):
                assert resolve_on_behalf_verdict(action) is OnBehalfVerdict.AUTO_DRAFT

    def test_ask_blocks_colleague_visible_actions(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        for action in _NON_DRAFT_ACTIONS:
            with self.subTest(action=action):
                assert resolve_on_behalf_verdict(action) is OnBehalfVerdict.BLOCK


class TestDraftOrAskMode(_OnBehalfDbBase):
    def test_draft_form_action_auto_drafts(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "draft_or_ask")
        for action in _DRAFT_FORM_ACTIONS:
            with self.subTest(action=action):
                assert resolve_on_behalf_verdict(action) is OnBehalfVerdict.AUTO_DRAFT

    def test_non_draft_action_blocks(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "draft_or_ask")
        for action in _NON_DRAFT_ACTIONS:
            with self.subTest(action=action):
                assert resolve_on_behalf_verdict(action) is OnBehalfVerdict.BLOCK


class TestDraftExemptUnderEveryBlockingMode(_OnBehalfDbBase):
    """The draft carve-out is per-ACTION, not per-mode (#draft-bypass).

    The bug: ``post_draft_note`` BLOCKed under ASK. The fix makes a
    draft-form action exempt under BOTH blocking modes, so a draft post
    never needs approval regardless of which strict mode the user picked.
    """

    def test_draft_auto_drafts_under_both_blocking_modes(self) -> None:
        for mode in ("ask", "draft_or_ask"):
            for action in _DRAFT_FORM_ACTIONS:
                with self.subTest(mode=mode, action=action):
                    ConfigSetting.objects.set_value("on_behalf_post_mode", mode)
                    assert resolve_on_behalf_verdict(action) is OnBehalfVerdict.AUTO_DRAFT


class TestAutoActionsAllowlist(_OnBehalfDbBase):
    """An action in ``on_behalf_auto_actions`` PROCEEDs under every blocking mode.

    These are the user's routine self-documentation on their OWN ticket (E2E
    evidence), not a colleague-facing voice — so the gate auto-proceeds them
    without an approval, identical to IMMEDIATE for that one action.
    """

    def test_auto_action_proceeds_under_both_blocking_modes(self) -> None:
        for mode in ("ask", "draft_or_ask"):
            for action in _AUTO_ACTIONS:
                with self.subTest(mode=mode, action=action):
                    ConfigSetting.objects.set_value("on_behalf_post_mode", mode)
                    assert resolve_on_behalf_verdict(action) is OnBehalfVerdict.PROCEED

    def test_colleague_visible_action_still_blocks(self) -> None:
        for mode in ("ask", "draft_or_ask"):
            with self.subTest(mode=mode):
                ConfigSetting.objects.set_value("on_behalf_post_mode", mode)
                assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK

    def test_default_allowlist_includes_post_e2e_evidence(self) -> None:
        """No explicit ``on_behalf_auto_actions`` → the default carve-out still applies."""
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        assert resolve_on_behalf_verdict("post_e2e_evidence") is OnBehalfVerdict.PROCEED

    def test_empty_allowlist_re_gates_evidence(self) -> None:
        """A user can clear the allowlist to re-gate evidence under a blocking mode."""
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        ConfigSetting.objects.set_value("on_behalf_auto_actions", [])
        assert resolve_on_behalf_verdict("post_e2e_evidence") is OnBehalfVerdict.BLOCK

    def test_custom_allowlist_overrides_default(self) -> None:
        """An explicit allowlist replaces the default — evidence re-gates, the named action proceeds."""
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        ConfigSetting.objects.set_value("on_behalf_auto_actions", ["post_comment"])
        assert resolve_on_behalf_verdict("post_e2e_evidence") is OnBehalfVerdict.BLOCK
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.PROCEED

    def test_per_overlay_allowlist_override(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        ConfigSetting.objects.set_value("on_behalf_auto_actions", [], scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        assert resolve_on_behalf_verdict("post_e2e_evidence") is OnBehalfVerdict.BLOCK

    def test_env_allowlist_override(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        self.monkeypatch.setenv("T3_ON_BEHALF_AUTO_ACTIONS", "")
        assert resolve_on_behalf_verdict("post_e2e_evidence") is OnBehalfVerdict.BLOCK


class TestReviewRequestPostDrivenByTier(_OnBehalfDbBase):
    """Review-request blocking is driven off the autonomy TIER, not a side flag.

    The deleted parallel flag ``agent_review_request_disabled`` is replaced by the
    resolved bool ``review_request_post_disabled`` that ``_apply_autonomy`` sets
    per-tier: ``notify`` → True (BLOCK — customer overlays keep the human in the
    review loop), ``full`` → False (PROCEED — solo tooling overlays auto-request),
    ``babysit`` → default False (review-request follows ``on_behalf_post_mode``).
    An explicit per-overlay pin of ``review_request_post_disabled`` always wins.
    """

    def test_notify_tier_blocks_review_request_under_immediate(self) -> None:
        # The ``notify`` collapse sets ``on_behalf_post_mode = immediate`` AND the
        # resolved ``review_request_post_disabled = True`` — so review-request
        # BLOCKs even though every other post would proceed under immediate.
        ConfigSetting.objects.set_value("autonomy", "notify", scope="customer")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "customer")
        assert resolve_on_behalf_verdict("review_request_post") is OnBehalfVerdict.BLOCK

    def test_notify_block_is_scoped_to_review_request_only(self) -> None:
        # The tier disable must NOT collapse every colleague-visible action — only
        # the review-request post. Other ``immediate`` posts keep proceeding.
        ConfigSetting.objects.set_value("autonomy", "notify", scope="customer")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "customer")
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.PROCEED

    def test_full_tier_proceeds_review_request(self) -> None:
        # A ``full`` overlay is a solo tooling surface — review-request PROCEEDs.
        ConfigSetting.objects.set_value("autonomy", "full", scope="tooling")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "tooling")
        assert resolve_on_behalf_verdict("review_request_post") is OnBehalfVerdict.PROCEED

    def test_babysit_review_request_follows_on_behalf_post_mode_immediate(self) -> None:
        # Under ``babysit`` review-request is gated by ``on_behalf_post_mode`` like
        # any other colleague-visible post: ``immediate`` → PROCEED.
        ConfigSetting.objects.set_value("autonomy", "babysit", scope="careful")
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate", scope="careful")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "careful")
        assert resolve_on_behalf_verdict("review_request_post") is OnBehalfVerdict.PROCEED

    def test_babysit_review_request_follows_on_behalf_post_mode_ask(self) -> None:
        # Under ``babysit`` a blocking ``on_behalf_post_mode`` BLOCKs review-request
        # exactly like any other gated post.
        ConfigSetting.objects.set_value("autonomy", "babysit", scope="careful")
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask", scope="careful")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "careful")
        assert resolve_on_behalf_verdict("review_request_post") is OnBehalfVerdict.BLOCK

    def test_explicit_pin_blocks_review_request_on_full_overlay(self) -> None:
        # The Option-A per-overlay escape: an explicit ``review_request_post_disabled``
        # pin wins over the ``full`` tier's PROCEED default.
        ConfigSetting.objects.set_value("autonomy", "full", scope="tooling")
        ConfigSetting.objects.set_value("review_request_post_disabled", value=True, scope="tooling")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "tooling")
        assert resolve_on_behalf_verdict("review_request_post") is OnBehalfVerdict.BLOCK

    def test_explicit_pin_proceeds_review_request_on_notify_overlay(self) -> None:
        # The mirror escape: an explicit ``review_request_post_disabled = False``
        # pin wins over the ``notify`` tier's BLOCK default — the overlay opts back
        # into auto-request despite running ``notify``.
        ConfigSetting.objects.set_value("autonomy", "notify", scope="customer")
        ConfigSetting.objects.set_value("review_request_post_disabled", value=False, scope="customer")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "customer")
        assert resolve_on_behalf_verdict("review_request_post") is OnBehalfVerdict.PROCEED

    def test_default_off_lets_immediate_review_request_proceed(self) -> None:
        # No autonomy tier and no pin → default False → ``immediate`` review-request
        # PROCEEDs, exactly the legacy babysit behaviour.
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        assert resolve_on_behalf_verdict("review_request_post") is OnBehalfVerdict.PROCEED

    def test_per_overlay_notify_blocks_only_that_overlay(self) -> None:
        # The customer-overlay scenario: a solo tooling overlay runs ``full`` and
        # auto-requests; the customer overlay runs ``notify`` and BLOCKs.
        ConfigSetting.objects.set_value("autonomy", "notify", scope="customer")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "customer")
        assert resolve_on_behalf_verdict("review_request_post") is OnBehalfVerdict.BLOCK

    def test_other_overlay_unaffected_by_per_overlay_notify(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "notify", scope="customer")
        ConfigSetting.objects.set_value("autonomy", "full", scope="tooling")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "tooling")
        assert resolve_on_behalf_verdict("review_request_post") is OnBehalfVerdict.PROCEED


class TestDefaults(_OnBehalfDbBase):
    def test_default_when_no_config(self) -> None:
        """No DB row → DRAFT_OR_ASK (the dataclass default)."""
        assert resolve_on_behalf_verdict("post_draft_note") is OnBehalfVerdict.AUTO_DRAFT
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK


class TestParseInvalid:
    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid on_behalf_post_mode"):
            OnBehalfPostMode.parse("bogus")

    def test_parse_normalises_case_and_whitespace(self) -> None:
        assert OnBehalfPostMode.parse("  IMMEDIATE  ") is OnBehalfPostMode.IMMEDIATE
        assert OnBehalfPostMode.parse("Ask") is OnBehalfPostMode.ASK


class TestRetiredLegacyTomlAlias(_OnBehalfDbBase):
    """The legacy ``[teatree] ask_before_post_on_behalf`` TOML shim is RETIRED (#1775).

    ``on_behalf_post_mode`` is now DB-home and ``ask_before_post_on_behalf`` is a
    DERIVED value, so the pre-partition shim that translated the legacy boolean
    TOML key into a mode is gone: the key is ignored on read. A config that still
    carries it resolves to the DB-store value (or the DRAFT_OR_ASK default) — the
    user migrates it with ``config_setting import`` / ``config_setting set``.
    """

    def test_legacy_true_is_ignored_falls_through_to_default(self) -> None:
        # DRAFT_OR_ASK default: visible posts BLOCK, drafts AUTO_DRAFT
        # (this happens to coincide with the old ASK mapping).
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK
        assert resolve_on_behalf_verdict("post_draft_note") is OnBehalfVerdict.AUTO_DRAFT

    def test_legacy_false_is_ignored_does_not_open_the_gate(self) -> None:
        # The gate stays at the DRAFT_OR_ASK default and visible posts still
        # BLOCK. The user sets the DB-home mode instead.
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK

    def test_db_mode_wins_over_legacy_toml_key(self) -> None:
        """A stored ``on_behalf_post_mode`` row resolves as the DB-home value."""
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.PROCEED


class TestOverlayOverride(_OnBehalfDbBase):
    def test_per_overlay_override_wins(self) -> None:
        """A trusted overlay can opt into IMMEDIATE without flipping the global.

        ``on_behalf_post_mode`` is DB-home, so the global value is a GLOBAL-scope
        row and the per-overlay opinion is an OVERLAY-scoped row that beats it.
        """
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate", scope="trusted")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.PROCEED


class TestEnvOverride(_OnBehalfDbBase):
    def test_env_wins_over_db(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        self.monkeypatch.setenv("T3_ON_BEHALF_POST_MODE", "immediate")
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.PROCEED

    def test_env_invalid_raises(self) -> None:
        self.monkeypatch.setenv("T3_ON_BEHALF_POST_MODE", "bogus")
        with pytest.raises(ValueError, match="Invalid on_behalf_post_mode"):
            resolve_on_behalf_verdict("post_comment")
