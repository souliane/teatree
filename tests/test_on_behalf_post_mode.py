"""Pure-resolver tests for ``resolve_on_behalf_verdict(action)``.

No Django, no HTTP, no ORM. TOML fixtures under ``tmp_path`` with
``teatree.config.CONFIG_PATH`` monkeypatched — the matrix of (mode,
action) → verdict only exercises :mod:`teatree.config` +
:mod:`teatree.on_behalf_gate`. Mirrors :mod:`tests.test_on_behalf_gate`
in style.
"""

from pathlib import Path

import pytest

from teatree.config import OnBehalfPostMode
from teatree.on_behalf_gate import OnBehalfVerdict, resolve_on_behalf_verdict


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / ".teatree.toml"
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    return cfg


def _write(cfg: Path, body: str) -> None:
    cfg.write_text(body, encoding="utf-8")


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


class TestImmediateMode:
    @pytest.mark.parametrize("action", [*_DRAFT_FORM_ACTIONS, *_NON_DRAFT_ACTIONS])
    def test_immediate_always_passes(self, config_file: Path, action: str) -> None:
        _write(config_file, '[teatree]\non_behalf_post_mode = "immediate"\n')
        assert resolve_on_behalf_verdict(action) is OnBehalfVerdict.PROCEED


class TestAskMode:
    """ASK blocks colleague-VISIBLE posts but EXEMPTS drafts (#draft-bypass).

    A draft is colleague-invisible, so it never needs approval — even
    under strict ASK it resolves to AUTO_DRAFT, identical to DRAFT_OR_ASK.
    Only colleague-visible actions BLOCK.
    """

    @pytest.mark.parametrize("action", _DRAFT_FORM_ACTIONS)
    def test_ask_exempts_draft_form_actions(self, config_file: Path, action: str) -> None:
        _write(config_file, '[teatree]\non_behalf_post_mode = "ask"\n')
        assert resolve_on_behalf_verdict(action) is OnBehalfVerdict.AUTO_DRAFT

    @pytest.mark.parametrize("action", _NON_DRAFT_ACTIONS)
    def test_ask_blocks_colleague_visible_actions(self, config_file: Path, action: str) -> None:
        _write(config_file, '[teatree]\non_behalf_post_mode = "ask"\n')
        assert resolve_on_behalf_verdict(action) is OnBehalfVerdict.BLOCK


class TestDraftOrAskMode:
    @pytest.mark.parametrize("action", _DRAFT_FORM_ACTIONS)
    def test_draft_form_action_auto_drafts(self, config_file: Path, action: str) -> None:
        _write(config_file, '[teatree]\non_behalf_post_mode = "draft_or_ask"\n')
        assert resolve_on_behalf_verdict(action) is OnBehalfVerdict.AUTO_DRAFT

    @pytest.mark.parametrize("action", _NON_DRAFT_ACTIONS)
    def test_non_draft_action_blocks(self, config_file: Path, action: str) -> None:
        _write(config_file, '[teatree]\non_behalf_post_mode = "draft_or_ask"\n')
        assert resolve_on_behalf_verdict(action) is OnBehalfVerdict.BLOCK


class TestDraftExemptUnderEveryBlockingMode:
    """The draft carve-out is per-ACTION, not per-mode (#draft-bypass).

    The bug: ``post_draft_note`` BLOCKed under ASK. The fix makes a
    draft-form action exempt under BOTH blocking modes, so a draft post
    never needs approval regardless of which strict mode the user picked.
    """

    @pytest.mark.parametrize("mode", ["ask", "draft_or_ask"])
    @pytest.mark.parametrize("action", _DRAFT_FORM_ACTIONS)
    def test_draft_auto_drafts_under_both_blocking_modes(self, config_file: Path, mode: str, action: str) -> None:
        _write(config_file, f'[teatree]\non_behalf_post_mode = "{mode}"\n')
        assert resolve_on_behalf_verdict(action) is OnBehalfVerdict.AUTO_DRAFT


class TestAutoActionsAllowlist:
    """An action in ``on_behalf_auto_actions`` PROCEEDs under every blocking mode.

    These are the user's routine self-documentation on their OWN ticket (E2E
    evidence), not a colleague-facing voice — so the gate auto-proceeds them
    without an approval, identical to IMMEDIATE for that one action.
    """

    @pytest.mark.parametrize("mode", ["ask", "draft_or_ask"])
    @pytest.mark.parametrize("action", _AUTO_ACTIONS)
    def test_auto_action_proceeds_under_both_blocking_modes(self, config_file: Path, mode: str, action: str) -> None:
        _write(config_file, f'[teatree]\non_behalf_post_mode = "{mode}"\n')
        assert resolve_on_behalf_verdict(action) is OnBehalfVerdict.PROCEED

    @pytest.mark.parametrize("mode", ["ask", "draft_or_ask"])
    def test_colleague_visible_action_still_blocks(self, config_file: Path, mode: str) -> None:
        _write(config_file, f'[teatree]\non_behalf_post_mode = "{mode}"\n')
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK

    def test_default_allowlist_includes_post_e2e_evidence(self, config_file: Path) -> None:
        """No explicit ``on_behalf_auto_actions`` → the default carve-out still applies."""
        _write(config_file, '[teatree]\non_behalf_post_mode = "ask"\n')
        assert resolve_on_behalf_verdict("post_e2e_evidence") is OnBehalfVerdict.PROCEED

    def test_empty_allowlist_re_gates_evidence(self, config_file: Path) -> None:
        """A user can clear the allowlist to re-gate evidence under a blocking mode."""
        _write(
            config_file,
            '[teatree]\non_behalf_post_mode = "ask"\non_behalf_auto_actions = []\n',
        )
        assert resolve_on_behalf_verdict("post_e2e_evidence") is OnBehalfVerdict.BLOCK

    def test_custom_allowlist_overrides_default(self, config_file: Path) -> None:
        """An explicit allowlist replaces the default — evidence re-gates, the named action proceeds."""
        _write(
            config_file,
            '[teatree]\non_behalf_post_mode = "ask"\non_behalf_auto_actions = ["post_comment"]\n',
        )
        assert resolve_on_behalf_verdict("post_e2e_evidence") is OnBehalfVerdict.BLOCK
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.PROCEED

    def test_per_overlay_allowlist_override(self, config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(
            config_file,
            "[teatree]\n"
            'on_behalf_post_mode = "ask"\n'
            "[overlays.trusted]\n"
            'overlay_class = "x.Y"\n'
            "on_behalf_auto_actions = []\n",
        )
        monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        assert resolve_on_behalf_verdict("post_e2e_evidence") is OnBehalfVerdict.BLOCK

    def test_env_allowlist_override(self, config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(config_file, '[teatree]\non_behalf_post_mode = "ask"\n')
        monkeypatch.setenv("T3_ON_BEHALF_AUTO_ACTIONS", "")
        assert resolve_on_behalf_verdict("post_e2e_evidence") is OnBehalfVerdict.BLOCK


class TestDefaults:
    def test_default_when_no_config(self, config_file: Path) -> None:
        """No file at all → DRAFT_OR_ASK (the new default)."""
        # config_file fixture monkeypatches CONFIG_PATH but doesn't write
        # the file, so load_config() returns the dataclass defaults.
        assert resolve_on_behalf_verdict("post_draft_note") is OnBehalfVerdict.AUTO_DRAFT
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK

    def test_default_when_section_present_but_unset(self, config_file: Path) -> None:
        _write(config_file, "[teatree]\n")
        assert resolve_on_behalf_verdict("post_draft_note") is OnBehalfVerdict.AUTO_DRAFT
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK


class TestParseInvalid:
    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid on_behalf_post_mode"):
            OnBehalfPostMode.parse("bogus")

    def test_parse_normalises_case_and_whitespace(self) -> None:
        assert OnBehalfPostMode.parse("  IMMEDIATE  ") is OnBehalfPostMode.IMMEDIATE
        assert OnBehalfPostMode.parse("Ask") is OnBehalfPostMode.ASK


class TestBackwardCompatibilityAlias:
    """``ask_before_post_on_behalf = true/false`` in toml maps to the mode."""

    def test_legacy_true_maps_to_ask(self, config_file: Path) -> None:
        _write(config_file, "[teatree]\nask_before_post_on_behalf = true\n")
        # Maps to ASK: colleague-visible actions block.
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK
        # A draft-form action is exempt under ASK too — it auto-drafts.
        assert resolve_on_behalf_verdict("post_draft_note") is OnBehalfVerdict.AUTO_DRAFT

    def test_legacy_false_maps_to_immediate(self, config_file: Path) -> None:
        _write(config_file, "[teatree]\nask_before_post_on_behalf = false\n")
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.PROCEED
        assert resolve_on_behalf_verdict("post_draft_note") is OnBehalfVerdict.PROCEED

    def test_legacy_absent_defaults_to_draft_or_ask(self, config_file: Path) -> None:
        _write(config_file, "[teatree]\n")
        assert resolve_on_behalf_verdict("post_draft_note") is OnBehalfVerdict.AUTO_DRAFT
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK

    def test_explicit_new_setting_wins_over_legacy(self, config_file: Path) -> None:
        """Explicit ``on_behalf_post_mode`` overrides any legacy boolean present."""
        _write(
            config_file,
            '[teatree]\nask_before_post_on_behalf = true\non_behalf_post_mode = "immediate"\n',
        )
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.PROCEED


class TestOverlayOverride:
    def test_per_overlay_override_wins(self, config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A trusted overlay can opt into IMMEDIATE without flipping the global."""
        _write(
            config_file,
            "[teatree]\n"
            'on_behalf_post_mode = "ask"\n'
            "[overlays.trusted]\n"
            'overlay_class = "x.Y"\n'
            'on_behalf_post_mode = "immediate"\n',
        )
        monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.PROCEED


class TestEnvOverride:
    def test_env_wins_over_toml(self, config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(config_file, '[teatree]\non_behalf_post_mode = "ask"\n')
        monkeypatch.setenv("T3_ON_BEHALF_POST_MODE", "immediate")
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.PROCEED

    def test_env_invalid_raises(self, config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(config_file, "[teatree]\n")
        monkeypatch.setenv("T3_ON_BEHALF_POST_MODE", "bogus")
        with pytest.raises(ValueError, match="Invalid on_behalf_post_mode"):
            resolve_on_behalf_verdict("post_comment")
