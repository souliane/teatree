"""Tests for the tri-state on-behalf posting pre-gate policy.

Integration-first per the Test-Writing Doctrine: real TOML fixtures under
``tmp_path`` with ``teatree.config.CONFIG_PATH`` monkeypatched to them.
No mocks — ``load_config`` / ``get_effective_settings`` exercised end-to-end.

The fine-grained (mode, action) → verdict matrix lives in
``tests/test_on_behalf_post_mode.py``; this file focuses on the
deprecation shim and the legacy alias surface so old user configs keep
working.
"""

import warnings
from pathlib import Path

import pytest

from teatree.config import OnBehalfPostMode
from teatree.on_behalf_gate import OnBehalfVerdict, ask_before_post_on_behalf_enabled, resolve_on_behalf_verdict


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / ".teatree.toml"
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    return cfg


def _write(cfg: Path, body: str) -> None:
    cfg.write_text(body, encoding="utf-8")


class TestNewDefaultMode:
    """The new default is DRAFT_OR_ASK (replaces the old default-true bool)."""

    def test_default_when_no_config_is_draft_or_ask(self, config_file: Path) -> None:
        assert resolve_on_behalf_verdict("post_draft_note") is OnBehalfVerdict.AUTO_DRAFT
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK

    def test_default_when_section_present_but_unset(self, config_file: Path) -> None:
        _write(config_file, "[teatree]\n")
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK


class TestExplicitModes:
    def test_explicit_immediate_disables_the_gate(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\non_behalf_post_mode = "immediate"\n')
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.PROCEED

    def test_explicit_ask_blocks_visible_posts_but_exempts_drafts(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\non_behalf_post_mode = "ask"\n')
        # Colleague-visible post: BLOCKed under ASK.
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK
        # Draft is colleague-invisible: EXEMPT even under ASK — it auto-drafts.
        assert resolve_on_behalf_verdict("post_draft_note") is OnBehalfVerdict.AUTO_DRAFT


class TestPerOverlayOverride:
    def test_per_overlay_override_wins_over_global(self, config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


class TestDeprecatedShim:
    """``ask_before_post_on_behalf_enabled()`` is kept for one release."""

    def test_returns_true_under_ask(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\non_behalf_post_mode = "ask"\n')
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert ask_before_post_on_behalf_enabled() is True

    def test_returns_true_under_draft_or_ask(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\non_behalf_post_mode = "draft_or_ask"\n')
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert ask_before_post_on_behalf_enabled() is True

    def test_returns_false_under_immediate(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\non_behalf_post_mode = "immediate"\n')
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert ask_before_post_on_behalf_enabled() is False

    def test_emits_deprecation_warning(self, config_file: Path) -> None:
        _write(config_file, "[teatree]\n")
        # Reset the module-level once-flag so this test sees the warning.
        import teatree.on_behalf_gate as gate_mod  # noqa: PLC0415

        gate_mod._DEPRECATION_EMITTED = False
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            ask_before_post_on_behalf_enabled()
        assert any(
            issubclass(w.category, DeprecationWarning) and "resolve_on_behalf_verdict" in str(w.message) for w in caught
        )


class TestLegacyTomlAlias:
    """``ask_before_post_on_behalf = true/false`` keeps working for one release."""

    def test_legacy_true_blocks_visible_posts_but_exempts_drafts(self, config_file: Path) -> None:
        _write(config_file, "[teatree]\nask_before_post_on_behalf = true\n")
        # Maps to ASK: a colleague-visible post blocks.
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK
        # A draft is colleague-invisible — exempt under ASK too.
        assert resolve_on_behalf_verdict("post_draft_note") is OnBehalfVerdict.AUTO_DRAFT

    def test_legacy_false_passes_every_action(self, config_file: Path) -> None:
        _write(config_file, "[teatree]\nask_before_post_on_behalf = false\n")
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.PROCEED

    def test_no_legacy_no_new_setting_defaults_to_draft_or_ask(self, config_file: Path) -> None:
        _write(config_file, "[teatree]\n")
        # The dataclass default is DRAFT_OR_ASK.
        from teatree.config import load_config  # noqa: PLC0415

        assert load_config().user.on_behalf_post_mode is OnBehalfPostMode.DRAFT_OR_ASK
