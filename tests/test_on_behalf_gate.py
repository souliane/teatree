"""Tests for the tri-state on-behalf posting pre-gate policy.

``on_behalf_post_mode`` is DB-home (#1775): its sole authoritative tier is the
``ConfigSetting`` store (+ ``T3_*`` env). A ``[teatree]`` / ``[overlays.<name>]``
TOML value for it is ignored on read, so every mode is staged via
``ConfigSetting.objects.set_value`` rather than TOML. ``get_effective_settings``
is exercised end-to-end with no mocks. ``CONFIG_PATH`` is monkeypatched to an
isolated (unwritten) file so the real ``~/.teatree.toml`` never leaks in.

The fine-grained (mode, action) → verdict matrix lives in
``tests/test_on_behalf_post_mode.py``; this file focuses on the deprecation
shim and the retirement of the legacy ``ask_before_post_on_behalf`` TOML alias.
"""

import warnings
from pathlib import Path

import pytest
from django.test import TestCase

from teatree.config import OnBehalfPostMode
from teatree.core.models import ConfigSetting
from teatree.on_behalf_gate import OnBehalfVerdict, ask_before_post_on_behalf_enabled, resolve_on_behalf_verdict


class _OnBehalfDbBase(TestCase):
    """Isolate ``CONFIG_PATH`` and the on-behalf env so the DB store is the sole tier."""

    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config_path = tmp_path / ".teatree.toml"
        monkeypatch.setattr("teatree.config.CONFIG_PATH", self.config_path)
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


class TestDeprecatedShim(_OnBehalfDbBase):
    """``ask_before_post_on_behalf_enabled()`` is kept for one release."""

    def test_returns_true_under_ask(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert ask_before_post_on_behalf_enabled() is True

    def test_returns_true_under_draft_or_ask(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "draft_or_ask")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert ask_before_post_on_behalf_enabled() is True

    def test_returns_false_under_immediate(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert ask_before_post_on_behalf_enabled() is False

    def test_emits_deprecation_warning(self) -> None:
        # Reset the module-level once-flag so this test sees the warning.
        import teatree.on_behalf_gate as gate_mod  # noqa: PLC0415

        gate_mod._DEPRECATION_EMITTED = False
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            ask_before_post_on_behalf_enabled()
        assert any(
            issubclass(w.category, DeprecationWarning) and "resolve_on_behalf_verdict" in str(w.message) for w in caught
        )


class TestRetiredLegacyTomlAlias(_OnBehalfDbBase):
    """The legacy ``ask_before_post_on_behalf`` TOML alias is RETIRED (#1775).

    The pre-partition shim that translated the boolean TOML key into a mode is
    gone: ``on_behalf_post_mode`` is DB-home and the key is ignored on read, so a
    config that still carries it resolves to the DB-store value (or the
    DRAFT_OR_ASK default). The user migrates it with ``config_setting import``.
    """

    def test_legacy_true_is_ignored_falls_through_to_default(self) -> None:
        self.config_path.write_text("[teatree]\nask_before_post_on_behalf = true\n", encoding="utf-8")
        # Ignored → DRAFT_OR_ASK default (coincides with the old ASK mapping here).
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK
        assert resolve_on_behalf_verdict("post_draft_note") is OnBehalfVerdict.AUTO_DRAFT

    def test_legacy_false_is_ignored_does_not_open_the_gate(self) -> None:
        # The retired shim no longer maps ``false`` → IMMEDIATE: the gate stays
        # at the DRAFT_OR_ASK default and a visible post still BLOCKs.
        self.config_path.write_text("[teatree]\nask_before_post_on_behalf = false\n", encoding="utf-8")
        assert resolve_on_behalf_verdict("post_comment") is OnBehalfVerdict.BLOCK

    def test_no_db_row_defaults_to_draft_or_ask(self) -> None:
        from teatree.config import get_effective_settings  # noqa: PLC0415

        assert get_effective_settings().on_behalf_post_mode is OnBehalfPostMode.DRAFT_OR_ASK
