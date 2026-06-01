"""The single per-overlay ``autonomy`` switch (souliane/teatree#1668).

One coherent value — ``autonomy = "full"`` vs ``"babysit"`` (default) — that
governs the whole USER-in-the-loop approval surface for an overlay. When
``full`` resolves for the active overlay, the three scattered approval gates
(``on_behalf_post_mode``, ``require_human_approval_to_merge``,
``require_human_approval_to_answer``) collapse to their autonomous value in
``get_effective_settings`` UNLESS the user pinned an explicit per-gate
override (explicit always wins — autonomy never silently overrides an opinion).

The safety/quality floor is out of scope by construction: ``autonomy`` only
fills those three fields. ``privacy`` / banned-terms / cold-review /
never-lockout / the substrate ``--human-authorize`` keystone are untouched.
"""

from pathlib import Path

import pytest

from teatree.config import Autonomy, Mode, OnBehalfPostMode, get_effective_settings, load_config

from ._shared import _write_toml


class TestAutonomyParse:
    def test_parse_full(self) -> None:
        assert Autonomy.parse("full") is Autonomy.FULL

    def test_parse_babysit(self) -> None:
        assert Autonomy.parse("babysit") is Autonomy.BABYSIT

    def test_parse_is_case_insensitive(self) -> None:
        assert Autonomy.parse("  FULL ") is Autonomy.FULL

    def test_parse_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid autonomy"):
            Autonomy.parse("yolo")


class TestAutonomyDefault:
    def test_defaults_to_babysit(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\n")
        assert load_config(config_path).user.autonomy is Autonomy.BABYSIT

    def test_babysit_keeps_conservative_gate_values(self, tmp_path: Path) -> None:
        """Default (babysit) leaves every gate at its conservative default."""
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\n")
        user = load_config(config_path).user
        assert user.on_behalf_post_mode is OnBehalfPostMode.DRAFT_OR_ASK
        assert user.require_human_approval_to_merge is True
        assert user.require_human_approval_to_answer is True


class TestAutonomyFullResolution:
    """``autonomy = "full"`` collapses the three gates to autonomous values."""

    def test_per_overlay_full_flips_all_three_gates(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        monkeypatch.delenv("T3_ON_BEHALF_POST_MODE", raising=False)
        _write_toml(
            config_file,
            '[teatree]\n[overlays.trusted]\nmode = "auto"\nautonomy = "full"\n',
        )

        settings = get_effective_settings()
        assert settings.autonomy is Autonomy.FULL
        assert settings.on_behalf_post_mode is OnBehalfPostMode.IMMEDIATE
        assert settings.require_human_approval_to_merge is False
        assert settings.require_human_approval_to_answer is False

    def test_full_leaves_safety_floor_untouched(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Autonomy governs only the three USER-approval gates — never the floor."""
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        _write_toml(
            config_file,
            '[teatree]\nprivacy = "strict"\n[overlays.trusted]\nmode = "auto"\nautonomy = "full"\n',
        )

        settings = get_effective_settings()
        assert settings.privacy == "strict"
        # never-lockout / self-rescue posture (the bash gate) is not touched.
        assert settings.orchestrator_bash_gate_enabled is True

    def test_explicit_per_gate_override_wins_over_full(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An explicit per-gate value beats the autonomy collapse — opinions win."""
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        monkeypatch.delenv("T3_ON_BEHALF_POST_MODE", raising=False)
        _write_toml(
            config_file,
            '[teatree]\n[overlays.trusted]\nmode = "auto"\nautonomy = "full"\nrequire_human_approval_to_merge = true\n',
        )

        settings = get_effective_settings()
        # full still flips the two un-pinned gates ...
        assert settings.on_behalf_post_mode is OnBehalfPostMode.IMMEDIATE
        assert settings.require_human_approval_to_answer is False
        # ... but the explicitly-pinned gate keeps the user's value.
        assert settings.require_human_approval_to_merge is True

    def test_babysit_overlay_keeps_gates_blocking(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_OVERLAY_NAME", "careful")
        monkeypatch.delenv("T3_ON_BEHALF_POST_MODE", raising=False)
        _write_toml(
            config_file,
            '[teatree]\n[overlays.careful]\nmode = "auto"\nautonomy = "babysit"\n',
        )

        settings = get_effective_settings()
        assert settings.autonomy is Autonomy.BABYSIT
        assert settings.on_behalf_post_mode is OnBehalfPostMode.DRAFT_OR_ASK
        assert settings.require_human_approval_to_merge is True
        assert settings.require_human_approval_to_answer is True

    def test_one_overlay_full_does_not_leak_to_another(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Per-overlay isolation: full on one overlay never relaxes another."""
        del elsewhere, no_installed_overlays
        _write_toml(
            config_file,
            "[teatree]\n"
            '[overlays.trusted]\nmode = "auto"\nautonomy = "full"\n'
            '[overlays.careful]\nmode = "auto"\nautonomy = "babysit"\n',
        )

        monkeypatch.setenv("T3_OVERLAY_NAME", "careful")
        careful = get_effective_settings()
        assert careful.require_human_approval_to_merge is True
        assert careful.on_behalf_post_mode is OnBehalfPostMode.DRAFT_OR_ASK

        monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        trusted = get_effective_settings()
        assert trusted.require_human_approval_to_merge is False
        assert trusted.on_behalf_post_mode is OnBehalfPostMode.IMMEDIATE

    def test_full_keeps_mode_auto_consistent(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``full`` implies ``mode = auto`` so merge autonomy is actually reached.

        ``require_human_approval_to_merge = false`` only unlocks the loop's
        auto-merge when ``mode == AUTO`` (see ``tick_jobs`` solo-overlay gate),
        so a ``full`` overlay that forgot ``mode`` would be a silent no-op.
        """
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        _write_toml(config_file, '[teatree]\n[overlays.trusted]\nautonomy = "full"\n')

        settings = get_effective_settings()
        assert settings.mode is Mode.AUTO
