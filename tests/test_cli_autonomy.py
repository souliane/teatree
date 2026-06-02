"""``t3 <overlay> autonomy`` — show / set the per-overlay trust switch.

The first-class CLI surface for the single ``autonomy`` knob (souliane/teatree
#1668) that collapses the three user-approval gates — colleague auto-approve
(``on_behalf_post_mode``), auto-merge (``require_human_approval_to_merge``),
and answer (``require_human_approval_to_answer``) — so a user flips an overlay
to full merge/approve autonomy with one command instead of hand-editing TOML.

Integration-first: a real ``~/.teatree.toml`` fixture under ``tmp_path`` with
``teatree.config.CONFIG_PATH`` monkeypatched, exercised through the typer
``CliRunner`` against the same ``autonomy`` subgroup the overlay app builder
attaches via :func:`teatree.cli.autonomy.register_autonomy_commands`, then the
persisted value is round-tripped through :func:`get_effective_settings` to
assert the gates collapse — and the safety floor does NOT.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from teatree.cli.autonomy import register_autonomy_commands
from teatree.config import Autonomy, Mode, OnBehalfPostMode, get_effective_settings

runner = CliRunner()


def _app() -> typer.Typer:
    app = typer.Typer()
    register_autonomy_commands(app)
    return app


class TestAutonomySetGlobal:
    def test_global_writes_teatree_table(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text("[teatree]\n", encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(_app(), ["autonomy", "set", "full", "--global"])
        assert result.exit_code == 0
        body = config_path.read_text(encoding="utf-8")
        assert 'autonomy = "full"' in body
        assert "[teatree]" in result.stdout

    def test_global_creates_config_when_absent(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(_app(), ["autonomy", "set", "notify", "--global"])
        assert result.exit_code == 0
        assert config_path.is_file()
        assert 'autonomy = "notify"' in config_path.read_text(encoding="utf-8")

    def test_global_preserves_other_keys(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text('[teatree]\nmode = "auto"\nbranch_prefix = "ac-"\n', encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            runner.invoke(_app(), ["autonomy", "set", "babysit", "--global"])
        body = config_path.read_text(encoding="utf-8")
        assert 'mode = "auto"' in body
        assert 'branch_prefix = "ac-"' in body
        assert 'autonomy = "babysit"' in body


class TestAutonomySetPerOverlay:
    def test_named_overlay_writes_overlays_table(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text("[teatree]\n", encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(_app(), ["autonomy", "set", "full", "--overlay", "t3-teatree"])
        assert result.exit_code == 0
        body = config_path.read_text(encoding="utf-8")
        assert "[overlays.t3-teatree]" in body
        assert 'autonomy = "full"' in body
        assert "[overlays.t3-teatree]" in result.stdout

    def test_named_overlay_preserves_sibling_overlay(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text(
            '[teatree]\n[overlays.t3-client]\nautonomy = "babysit"\nmode = "interactive"\n',
            encoding="utf-8",
        )
        with patch("teatree.config.CONFIG_PATH", config_path):
            runner.invoke(_app(), ["autonomy", "set", "full", "--overlay", "t3-teatree"])
        body = config_path.read_text(encoding="utf-8")
        # The sibling overlay's own knobs are untouched ...
        assert 'mode = "interactive"' in body
        # ... and its babysit value is preserved while the new overlay gets full.
        assert "[overlays.t3-client]" in body
        assert "[overlays.t3-teatree]" in body

    def test_defaults_to_active_overlay(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no --overlay, the value lands in the active overlay's table (real resolver)."""
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text('[teatree]\n[overlays.t3-active]\nmode = "auto"\n', encoding="utf-8")
        monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])
        monkeypatch.setenv("T3_OVERLAY_NAME", "t3-active")
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(_app(), ["autonomy", "set", "full"])
        assert result.exit_code == 0
        assert "[overlays.t3-active]" in config_path.read_text(encoding="utf-8")

    def test_no_active_overlay_and_no_target_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No --overlay, no --global, and no resolvable active overlay → refuse, write nothing."""
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text("[teatree]\n", encoding="utf-8")
        before = config_path.read_text(encoding="utf-8")
        # No installed overlays and a cwd free of manage.py → no active overlay resolves.
        monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        away = tmp_path / "no_manage"
        away.mkdir()
        monkeypatch.chdir(away)
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(_app(), ["autonomy", "set", "full"])
        assert result.exit_code == 1
        assert config_path.read_text(encoding="utf-8") == before


class TestAutonomySetValidation:
    def test_typo_is_rejected_and_writes_nothing(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text("[teatree]\n", encoding="utf-8")
        before = config_path.read_text(encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(_app(), ["autonomy", "set", "yolo", "--global"])
        assert result.exit_code == 1
        assert config_path.read_text(encoding="utf-8") == before


class TestAutonomyShow:
    def test_show_reports_effective_value(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text('[teatree]\nautonomy = "notify"\n', encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(_app(), ["autonomy", "show"])
        assert result.exit_code == 0
        assert result.stdout.strip() == Autonomy.NOTIFY.value

    def test_show_defaults_to_babysit_when_unset(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text("[teatree]\n", encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            result = runner.invoke(_app(), ["autonomy", "show"])
        assert result.exit_code == 0
        assert result.stdout.strip() == Autonomy.BABYSIT.value

    def test_show_is_read_only(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text('[teatree]\nautonomy = "full"\n', encoding="utf-8")
        before = config_path.read_text(encoding="utf-8")
        with patch("teatree.config.CONFIG_PATH", config_path):
            runner.invoke(_app(), ["autonomy", "show"])
        assert config_path.read_text(encoding="utf-8") == before


@pytest.fixture
def isolated_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch ``CONFIG_PATH``, blank installed overlays, and a manage.py-free cwd.

    Mirrors the ``config_file`` + ``no_installed_overlays`` + ``elsewhere``
    triple the package-local ``tests/config/conftest.py`` provides — replicated
    here because this module sits at the ``tests/`` root, outside that package.
    Returns the staged ``~/.teatree.toml`` path the test writes its fixture to.
    """
    cfg = tmp_path / ".teatree.toml"
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])
    away = tmp_path / "no_manage"
    away.mkdir()
    monkeypatch.chdir(away)
    monkeypatch.delenv("T3_ON_BEHALF_POST_MODE", raising=False)
    monkeypatch.delenv("T3_MODE", raising=False)
    return cfg


class TestAutonomyKnobCollapsesGatesNotFloor:
    """The knob the CLI persists flips the approval gates, never the safety floor.

    These round-trip through ``get_effective_settings`` after the CLI write so
    the must-ALLOW (autonomous colleague auto-approve + auto-merge) and the
    must-DENY (safety floor stays in force) outcomes are proven end-to-end, not
    just asserted on the raw toml.
    """

    def test_full_must_allow_colleague_autoapprove_and_automerge(
        self,
        isolated_resolution: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``autonomy set full`` collapses on-behalf-post (colleague approve) + merge gates."""
        isolated_resolution.write_text("[teatree]\n", encoding="utf-8")
        result = runner.invoke(_app(), ["autonomy", "set", "full", "--overlay", "trusted"])
        assert result.exit_code == 0

        monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        settings = get_effective_settings()
        # must-ALLOW: colleague auto-approve (on-behalf posts publish immediately)
        # and the loop's auto-merge are both unblocked.
        assert settings.on_behalf_post_mode is OnBehalfPostMode.IMMEDIATE
        assert settings.require_human_approval_to_merge is False
        assert settings.require_human_approval_to_answer is False
        # And the merge-autonomy path is actually reachable (gated on mode == AUTO).
        assert settings.mode is Mode.AUTO

    def test_full_must_deny_relaxing_the_safety_floor(
        self,
        isolated_resolution: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A full-autonomy overlay never relaxes privacy / never-lockout — the floor stays on."""
        isolated_resolution.write_text('[teatree]\nprivacy = "strict"\n', encoding="utf-8")
        runner.invoke(_app(), ["autonomy", "set", "full", "--overlay", "trusted"])

        monkeypatch.setenv("T3_OVERLAY_NAME", "trusted")
        settings = get_effective_settings()
        assert settings.privacy == "strict"
        # never-lockout / self-rescue posture (the orchestrator bash gate) untouched.
        assert settings.orchestrator_bash_gate_enabled is True

    def test_babysit_must_deny_autonomous_merge_and_approve(
        self,
        isolated_resolution: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``autonomy set babysit`` keeps every approval gate blocking (the conservative default)."""
        isolated_resolution.write_text('[teatree]\n[overlays.careful]\nmode = "auto"\n', encoding="utf-8")
        runner.invoke(_app(), ["autonomy", "set", "babysit", "--overlay", "careful"])

        monkeypatch.setenv("T3_OVERLAY_NAME", "careful")
        settings = get_effective_settings()
        assert settings.autonomy is Autonomy.BABYSIT
        # must-DENY: even with mode = auto, the gates stay blocking under babysit.
        assert settings.on_behalf_post_mode is OnBehalfPostMode.DRAFT_OR_ASK
        assert settings.require_human_approval_to_merge is True
        assert settings.require_human_approval_to_answer is True
