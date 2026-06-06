"""The single per-overlay ``autonomy`` switch (souliane/teatree#1668).

One coherent value — three tiers ``full > notify > babysit`` (default
``babysit``) — that governs the whole USER-in-the-loop approval surface for an
overlay. Under ``full`` OR ``notify`` the three scattered approval gates
(``on_behalf_post_mode``, ``require_human_approval_to_merge``,
``require_human_approval_to_answer``) collapse to their autonomous value in
``get_effective_settings`` and ``mode`` is pinned to ``auto``, UNLESS the user
pinned an explicit per-gate override (explicit always wins — autonomy never
silently overrides an opinion). ``notify`` additionally derives
``notify_on_behalf = True`` (the after-receipt DM forced on); ``full`` and
``babysit`` keep it ``False``.

Over-pin fix: a global ``[teatree] mode`` is a workspace default and does NOT
defeat the ``mode = auto`` pin; only a per-overlay ``mode`` does.

The safety/quality floor is out of scope by construction: ``autonomy`` only
fills those gates (+ ``mode`` + the derived ``notify_on_behalf``). ``privacy``
/ banned-terms / cold-review / never-lockout / the substrate
``--human-authorize`` keystone are untouched.
"""

from pathlib import Path

import pytest

from teatree.config import Autonomy, Mode, OnBehalfPostMode, get_effective_settings, load_config

from ._shared import _write_toml


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
        auto-merge when ``mode == AUTO`` (see ``scanner_factories`` solo-overlay gate),
        so a ``full`` overlay that forgot ``mode`` would be a silent no-op.
        """
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        _write_toml(config_file, '[teatree]\n[overlays.trusted]\nautonomy = "full"\n')

        settings = get_effective_settings()
        assert settings.mode is Mode.AUTO

    def test_full_keeps_notify_on_behalf_false(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``full`` is silent — it does NOT derive the after-receipt DM on."""
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        _write_toml(config_file, '[teatree]\n[overlays.trusted]\nautonomy = "full"\n')

        assert get_effective_settings().notify_on_behalf is False

    def test_babysit_keeps_notify_on_behalf_false(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_OVERLAY_NAME", "careful")
        _write_toml(config_file, '[teatree]\n[overlays.careful]\nautonomy = "babysit"\n')

        assert get_effective_settings().notify_on_behalf is False


class TestAutonomyNotifyTier:
    """``autonomy = "notify"`` collapses the same three gates as ``full`` AND derives the DM."""

    def test_notify_flips_the_same_three_gates_as_full(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_OVERLAY_NAME", "client")
        monkeypatch.delenv("T3_ON_BEHALF_POST_MODE", raising=False)
        _write_toml(config_file, '[teatree]\n[overlays.client]\nautonomy = "notify"\n')

        settings = get_effective_settings()
        assert settings.autonomy is Autonomy.NOTIFY
        assert settings.on_behalf_post_mode is OnBehalfPostMode.IMMEDIATE
        assert settings.require_human_approval_to_merge is False
        assert settings.require_human_approval_to_answer is False
        assert settings.mode is Mode.AUTO

    def test_notify_derives_notify_on_behalf_true(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The distinguishing field: ``notify`` forces the after-receipt DM on."""
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_OVERLAY_NAME", "client")
        _write_toml(config_file, '[teatree]\n[overlays.client]\nautonomy = "notify"\n')

        assert get_effective_settings().notify_on_behalf is True

    def test_notify_leaves_safety_floor_untouched(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_OVERLAY_NAME", "client")
        _write_toml(
            config_file,
            '[teatree]\nprivacy = "strict"\n[overlays.client]\nautonomy = "notify"\n',
        )

        settings = get_effective_settings()
        assert settings.privacy == "strict"
        assert settings.orchestrator_bash_gate_enabled is True

    def test_notify_isolated_from_full_overlay(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Per-overlay isolation across all three tiers in one config."""
        del elsewhere, no_installed_overlays
        _write_toml(
            config_file,
            '[teatree]\n[overlays.t3-teatree]\nautonomy = "full"\n[overlays.t3-client]\nautonomy = "notify"\n',
        )

        monkeypatch.setenv("T3_OVERLAY_NAME", "t3-teatree")
        teatree = get_effective_settings()
        assert teatree.autonomy is Autonomy.FULL
        assert teatree.notify_on_behalf is False

        monkeypatch.setenv("T3_OVERLAY_NAME", "t3-client")
        client = get_effective_settings()
        assert client.autonomy is Autonomy.NOTIFY
        assert client.notify_on_behalf is True
        assert client.require_human_approval_to_merge is False


class TestAutonomyOverPinFix:
    """A global ``[teatree] mode`` must NOT defeat the autonomy ``mode = auto`` pin (#1668)."""

    def test_global_interactive_mode_does_not_defeat_full_mode_auto(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        monkeypatch.delenv("T3_MODE", raising=False)
        _write_toml(
            config_file,
            '[teatree]\nmode = "interactive"\n[overlays.trusted]\nautonomy = "full"\n',
        )

        settings = get_effective_settings()
        # The over-pin bug: a common global ``mode = interactive`` used to pin
        # ``mode`` and leave the overlay half-autonomous (gates relaxed but the
        # merge path still gated on ``mode == AUTO``). The collapse must win.
        assert settings.mode is Mode.AUTO
        assert settings.require_human_approval_to_merge is False

    def test_global_interactive_mode_does_not_defeat_notify_mode_auto(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_OVERLAY_NAME", "client")
        monkeypatch.delenv("T3_MODE", raising=False)
        _write_toml(
            config_file,
            '[teatree]\nmode = "interactive"\n[overlays.client]\nautonomy = "notify"\n',
        )

        settings = get_effective_settings()
        assert settings.mode is Mode.AUTO

    def test_per_overlay_explicit_mode_still_wins(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A per-overlay ``[overlays.<n>].mode`` is a deliberate opinion — it wins."""
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        monkeypatch.delenv("T3_MODE", raising=False)
        _write_toml(
            config_file,
            '[teatree]\n[overlays.trusted]\nmode = "interactive"\nautonomy = "full"\n',
        )

        settings = get_effective_settings()
        # The user pinned this overlay's mode explicitly — autonomy must not
        # override a per-overlay opinion (the gates still collapse).
        assert settings.mode is Mode.INTERACTIVE
        assert settings.require_human_approval_to_merge is False

    def test_global_explicit_gate_still_wins_over_collapse(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A global explicit per-gate value is an opinion and still wins (unchanged)."""
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        _write_toml(
            config_file,
            '[teatree]\nrequire_human_approval_to_merge = true\n[overlays.trusted]\nautonomy = "full"\n',
        )

        settings = get_effective_settings()
        assert settings.require_human_approval_to_merge is True
