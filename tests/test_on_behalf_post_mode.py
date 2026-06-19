"""Resolver tests for ``resolve_on_behalf_verdict(action)``.

The matrix of (mode, action) → verdict exercises :mod:`teatree.config` +
:mod:`teatree.on_behalf_gate` end-to-end. ``on_behalf_post_mode`` and
``on_behalf_auto_actions`` are DB-home (#1775): their sole authoritative tier is
the ``ConfigSetting`` store (+ ``T3_*`` env). A ``[teatree]`` /
``[overlays.<name>]`` TOML value for either is ignored on read, so every mode /
allowlist is staged via ``ConfigSetting.objects.set_value`` rather than TOML.
``CONFIG_PATH`` is monkeypatched to an isolated (unwritten) file so the real
``~/.teatree.toml`` never leaks in. Mirrors :mod:`tests.test_on_behalf_gate`.
"""

from pathlib import Path

import pytest
from django.test import TestCase

from teatree.config import OnBehalfPostMode
from teatree.core.models import ConfigSetting
from teatree.on_behalf_gate import OnBehalfVerdict, resolve_on_behalf_verdict


class _OnBehalfDbBase(TestCase):
    """Isolate ``CONFIG_PATH`` and the on-behalf env so the DB store is the sole tier."""

    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = tmp_path / ".teatree.toml"
        monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
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


class TestAgentReviewRequestDisabled(_OnBehalfDbBase):
    """``agent_review_request_disabled`` BLOCKs ``review_request_post`` regardless of mode.

    The customer-overlay done-definition: for an overlay that keeps a human in
    the merge loop (``require_human_approval_to_merge = True``), the agent's job
    ends at "MR is mergeable + review-requestable" — it must NOT auto-request
    review. But the autonomy collapse (``notify``/``full``) sets
    ``on_behalf_post_mode = immediate``, which would otherwise lift the
    review-request post gate. This setting is the dedicated, mode-independent
    disable that the user opts the customer overlay into; default off preserves
    the legacy behaviour for every other overlay.
    """

    def test_disabled_blocks_review_request_even_under_immediate(self) -> None:
        # ``immediate`` is exactly the autonomy-collapsed value a customer overlay
        # (``notify`` tier) resolves to — yet review-request must still BLOCK.
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        ConfigSetting.objects.set_value("agent_review_request_disabled", value=True)
        assert resolve_on_behalf_verdict("review_request_post") is OnBehalfVerdict.BLOCK

    def test_disable_is_scoped_to_review_request_only(self) -> None:
        # The disable must NOT collapse every colleague-visible action — only the
        # review-request post. Other ``immediate`` posts keep proceeding.
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        ConfigSetting.objects.set_value("agent_review_request_disabled", value=True)
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.PROCEED

    def test_disabled_blocks_review_request_under_blocking_mode_too(self) -> None:
        # Under a blocking mode the review-request was already BLOCKed; the
        # setting must keep it BLOCKed (a recorded approval is still required).
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        ConfigSetting.objects.set_value("agent_review_request_disabled", value=True)
        assert resolve_on_behalf_verdict("review_request_post") is OnBehalfVerdict.BLOCK

    def test_default_off_lets_immediate_review_request_proceed(self) -> None:
        # No row set → default False → ``immediate`` review-request PROCEEDs,
        # exactly the legacy behaviour. This pins that the gate is opt-in.
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        assert resolve_on_behalf_verdict("review_request_post") is OnBehalfVerdict.PROCEED

    def test_per_overlay_disable_blocks_only_that_overlay(self) -> None:
        # The customer-overlay scenario: global gate off (a solo tooling overlay
        # auto-requests), the customer overlay opts into the disable.
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        ConfigSetting.objects.set_value("agent_review_request_disabled", value=True, scope="customer")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "customer")
        assert resolve_on_behalf_verdict("review_request_post") is OnBehalfVerdict.BLOCK

    def test_other_overlay_unaffected_by_per_overlay_disable(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        ConfigSetting.objects.set_value("agent_review_request_disabled", value=True, scope="customer")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "tooling")
        assert resolve_on_behalf_verdict("review_request_post") is OnBehalfVerdict.PROCEED


class TestDefaults(_OnBehalfDbBase):
    def test_default_when_no_config(self) -> None:
        """No DB row and no config file → DRAFT_OR_ASK (the dataclass default)."""
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
        self._write_legacy("true")
        # The legacy key is ignored → DRAFT_OR_ASK default: visible posts BLOCK,
        # drafts AUTO_DRAFT (this happens to coincide with the old ASK mapping).
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK
        assert resolve_on_behalf_verdict("post_draft_note") is OnBehalfVerdict.AUTO_DRAFT

    def test_legacy_false_is_ignored_does_not_open_the_gate(self) -> None:
        # The retired shim no longer maps ``false`` → IMMEDIATE: the key is
        # ignored, so the gate stays at the DRAFT_OR_ASK default and visible
        # posts still BLOCK. The user must set the DB-home mode instead.
        self._write_legacy("false")
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK

    def test_db_mode_wins_over_legacy_toml_key(self) -> None:
        """A stored ``on_behalf_post_mode`` row resolves; the legacy TOML key is ignored."""
        self._write_legacy("true")
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.PROCEED

    def _write_legacy(self, boolean: str) -> None:
        from teatree.config import CONFIG_PATH  # noqa: PLC0415

        CONFIG_PATH.write_text(f"[teatree]\nask_before_post_on_behalf = {boolean}\n", encoding="utf-8")


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
