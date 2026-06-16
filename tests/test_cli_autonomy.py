"""``t3 <overlay> autonomy`` — show / set the per-overlay trust switch.

The first-class CLI surface for the single ``autonomy`` knob (souliane/teatree
#1668) that collapses the three user-approval gates — colleague auto-approve
(``on_behalf_post_mode``), auto-merge (``require_human_approval_to_merge``),
and answer (``require_human_approval_to_answer``) — so a user flips an overlay
to full merge/approve autonomy with one command instead of hand-editing config.

``autonomy`` is DB-home (#1775): its sole authoritative tier is the
``ConfigSetting`` store, so ``set`` writes a DB ROW (the active/``--overlay``
overlay's OVERLAY-scoped row by default, the GLOBAL-scope row with ``--global``)
— a ``[teatree]`` / ``[overlays.<name>]`` TOML value is ignored on read.
Integration-first: the ``set`` write is asserted on the persisted
``ConfigSetting`` row and round-tripped through :func:`get_effective_settings`
so the gates collapse — and the safety floor does NOT.
"""

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from teatree.cli.autonomy import register_autonomy_commands
from teatree.config import Autonomy, Mode, OnBehalfPostMode, get_effective_settings
from teatree.core.models import ConfigSetting

runner = CliRunner()


def _app() -> typer.Typer:
    app = typer.Typer()
    register_autonomy_commands(app)
    return app


@pytest.mark.django_db
class TestAutonomySetGlobal:
    def test_global_writes_global_scope_row(self) -> None:
        result = runner.invoke(_app(), ["autonomy", "set", "full", "--global"])
        assert result.exit_code == 0
        assert ConfigSetting.objects.get_effective("autonomy") == Autonomy.FULL.value
        assert "global config store" in result.stdout

    def test_global_upserts_over_existing_row(self) -> None:
        ConfigSetting.objects.set_value("autonomy", Autonomy.BABYSIT.value)
        result = runner.invoke(_app(), ["autonomy", "set", "notify", "--global"])
        assert result.exit_code == 0
        assert ConfigSetting.objects.get_effective("autonomy") == Autonomy.NOTIFY.value

    def test_global_leaves_overlay_scope_untouched(self) -> None:
        ConfigSetting.objects.set_value("autonomy", Autonomy.NOTIFY.value, scope="t3-teatree")
        runner.invoke(_app(), ["autonomy", "set", "babysit", "--global"])
        assert ConfigSetting.objects.get_effective("autonomy") == Autonomy.BABYSIT.value
        # The overlay-scoped row is a distinct ``(scope, key)`` and is preserved.
        assert ConfigSetting.objects.get_effective("autonomy", scope="t3-teatree") == Autonomy.NOTIFY.value


@pytest.mark.django_db
class TestAutonomySetPerOverlay:
    def test_named_overlay_writes_overlay_scope_row(self) -> None:
        result = runner.invoke(_app(), ["autonomy", "set", "full", "--overlay", "t3-teatree"])
        assert result.exit_code == 0
        assert ConfigSetting.objects.get_effective("autonomy", scope="t3-teatree") == Autonomy.FULL.value
        assert "t3-teatree" in result.stdout

    def test_named_overlay_preserves_sibling_overlay(self) -> None:
        ConfigSetting.objects.set_value("autonomy", Autonomy.BABYSIT.value, scope="t3-client")
        runner.invoke(_app(), ["autonomy", "set", "full", "--overlay", "t3-teatree"])
        # The sibling overlay's own value is a distinct row and is preserved ...
        assert ConfigSetting.objects.get_effective("autonomy", scope="t3-client") == Autonomy.BABYSIT.value
        # ... while the new overlay gets full.
        assert ConfigSetting.objects.get_effective("autonomy", scope="t3-teatree") == Autonomy.FULL.value

    def test_defaults_to_active_overlay(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no --overlay, the value lands in the active overlay's scope (real resolver).

        Overlay discovery is RAW/TOML (#1775 KEEP-TOML): a bare
        ``[overlays.t3-active]`` table is what makes ``t3-active`` the resolvable
        active overlay. The autonomy *value* itself is DB-home, so the write
        lands as an overlay-scoped ``ConfigSetting`` row, not a TOML key.
        """
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text("[teatree]\n[overlays.t3-active]\n", encoding="utf-8")
        monkeypatch.setattr("teatree.config.CONFIG_PATH", config_path)
        monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])
        monkeypatch.setenv("T3_OVERLAY_NAME", "t3-active")
        result = runner.invoke(_app(), ["autonomy", "set", "full"])
        assert result.exit_code == 0
        assert ConfigSetting.objects.get_effective("autonomy", scope="t3-active") == Autonomy.FULL.value

    def test_no_active_overlay_and_no_target_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No --overlay, no --global, and no resolvable active overlay → refuse, write nothing."""
        # No installed overlays and a cwd free of manage.py → no active overlay resolves.
        monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        away = tmp_path / "no_manage"
        away.mkdir()
        monkeypatch.chdir(away)
        result = runner.invoke(_app(), ["autonomy", "set", "full"])
        assert result.exit_code == 1
        assert ConfigSetting.objects.count() == 0


@pytest.mark.django_db
class TestAutonomySetValidation:
    def test_typo_is_rejected_and_writes_nothing(self) -> None:
        result = runner.invoke(_app(), ["autonomy", "set", "yolo", "--global"])
        assert result.exit_code == 1
        assert ConfigSetting.objects.count() == 0


@pytest.mark.django_db
class TestAutonomyShow:
    def test_show_reports_effective_value(self) -> None:
        ConfigSetting.objects.set_value("autonomy", Autonomy.NOTIFY.value)
        result = runner.invoke(_app(), ["autonomy", "show"])
        assert result.exit_code == 0
        assert result.stdout.strip() == Autonomy.NOTIFY.value

    def test_show_defaults_to_babysit_when_unset(self) -> None:
        result = runner.invoke(_app(), ["autonomy", "show"])
        assert result.exit_code == 0
        assert result.stdout.strip() == Autonomy.BABYSIT.value

    def test_show_is_read_only(self) -> None:
        ConfigSetting.objects.set_value("autonomy", Autonomy.FULL.value)
        runner.invoke(_app(), ["autonomy", "show"])
        # ``show`` is a pure resolver read — no row is written or cleared.
        assert ConfigSetting.objects.count() == 1
        assert ConfigSetting.objects.get_effective("autonomy") == Autonomy.FULL.value


@pytest.fixture
def isolated_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Blank the TOML config, blank installed overlays, and a manage.py-free cwd.

    Mirrors the ``config_file`` + ``no_installed_overlays`` + ``elsewhere``
    triple the package-local ``tests/config/conftest.py`` provides — replicated
    here because this module sits at the ``tests/`` root, outside that package.
    The ``autonomy`` value itself is DB-home now (#1775): the staged
    ``~/.teatree.toml`` only carries the TOML-home knobs (``privacy``) a couple
    of cases assert the floor against. Returns the staged config path.
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


@pytest.mark.django_db
class TestAutonomyKnobCollapsesGatesNotFloor:
    """The knob the CLI persists flips the approval gates, never the safety floor.

    These round-trip through ``get_effective_settings`` after the CLI write so
    the must-ALLOW (autonomous colleague auto-approve + auto-merge) and the
    must-DENY (safety floor stays in force) outcomes are proven end-to-end, not
    just asserted on the raw store.
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
        isolated_resolution.write_text("[teatree]\n", encoding="utf-8")
        # ``mode = auto`` is a per-overlay opinion; under the partition it is the
        # overlay-scoped DB row, not a ``[overlays.<name>]`` TOML key.
        ConfigSetting.objects.set_value("mode", Mode.AUTO.value, scope="careful")
        runner.invoke(_app(), ["autonomy", "set", "babysit", "--overlay", "careful"])

        monkeypatch.setenv("T3_OVERLAY_NAME", "careful")
        settings = get_effective_settings()
        assert settings.autonomy is Autonomy.BABYSIT
        # must-DENY: even with mode = auto, the gates stay blocking under babysit.
        assert settings.on_behalf_post_mode is OnBehalfPostMode.DRAFT_OR_ASK
        assert settings.require_human_approval_to_merge is True
        assert settings.require_human_approval_to_answer is True
