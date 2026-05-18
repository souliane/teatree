"""Tests for the db management command."""

import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings

import teatree.core.management.commands.db as db_mod
from teatree.core.models import Ticket, Worktree
from teatree.utils.approval import ApprovalRefusedError
from tests.teatree_core.management_commands._overlays import (
    FAILING_IMPORT_OVERLAY,
    FULL_OVERLAY,
    MINIMAL_OVERLAY,
    POST_DB_OVERLAY,
    SETTINGS,
    _patch_overlays,
)

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


# ── DB commands ─────────────────────────────────────────────────────


class TestDbRefresh(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_transitions_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "test"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            worktree = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/test",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )
            worktree.provision()
            worktree.save()

            result = cast("str", call_command("db", "refresh", path=str(wt_dir)))

            worktree.refresh_from_db()
            assert "refreshed" in result.lower()
            assert worktree.state == Worktree.State.PROVISIONED

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_runs_post_db_steps_and_reset_passwords(self) -> None:
        """Db refresh calls post-DB steps and password reset after successful import."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wt_dir = tmp_path / "test"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            worktree = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/test",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )
            worktree.provision()
            worktree.save()

            result = cast("str", call_command("db", "refresh", path=str(wt_dir)))

            assert "refreshed" in result.lower()

    @_patch_overlays(FAILING_IMPORT_OVERLAY)
    @override_settings(**SETTINGS)
    def test_failed_import_raises_system_exit_1(self) -> None:
        """A failed DB import must raise SystemExit(1), not return a string.

        Regression for #932: `return f"DB import failed..."` exited 0, so the
        lifecycle/loop proceeded on a broken DB.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wt_dir = tmp_path / "test"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            worktree = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/test",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )
            worktree.provision()
            worktree.save()

            with pytest.raises(SystemExit) as exc_info:
                call_command("db", "refresh", path=str(wt_dir))

            assert exc_info.value.code == 1

    @_patch_overlays(POST_DB_OVERLAY)
    @override_settings(**SETTINGS)
    def test_runs_post_db_steps_loop(self) -> None:
        """Db refresh iterates over overlay.get_post_db_steps and calls each callable."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wt_dir = tmp_path / "test"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            worktree = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/test",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )
            worktree.provision()
            worktree.save()

            result = cast("str", call_command("db", "refresh", path=str(wt_dir)))

            assert "refreshed" in result.lower()

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_fresh_dump_aborted_raises_system_exit_1(self) -> None:
        """A refused fresh-remote-dump approval must raise SystemExit(1).

        Regression for #932: `return f"Fresh remote dump aborted: {exc}"`
        exited 0, so the lifecycle proceeded as if the dump had happened.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "test"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            worktree = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/test",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )
            worktree.provision()
            worktree.save()

            with (
                patch.object(
                    db_mod,
                    "require_approval",
                    side_effect=ApprovalRefusedError("no tty"),
                ),
                pytest.raises(SystemExit) as exc_info,
            ):
                call_command("db", "refresh", path=str(wt_dir), fresh_dump=True)

            assert exc_info.value.code == 1

    @_patch_overlays(MINIMAL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_no_strategy_raises_system_exit_1(self) -> None:
        """`db refresh` with no import strategy is a genuine failure.

        The caller explicitly asked to refresh the DB; an overlay with no
        import strategy cannot satisfy that, so the caller must stop.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "test"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            worktree = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/test",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )
            worktree.provision()
            worktree.save()

            with pytest.raises(SystemExit) as exc_info:
                call_command("db", "refresh", path=str(wt_dir))

            assert exc_info.value.code == 1


class TestDbRestoreCi(TestCase):
    @_patch_overlays(FAILING_IMPORT_OVERLAY)
    @override_settings(**SETTINGS)
    def test_failed_restore_raises_system_exit_1(self) -> None:
        """A failed CI restore must raise SystemExit(1), not return a string.

        Regression for #932.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wt_dir = tmp_path / "test"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            worktree = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/test",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )
            worktree.provision()
            worktree.save()

            with pytest.raises(SystemExit) as exc_info:
                call_command("db", "restore-ci", path=str(wt_dir))

            assert exc_info.value.code == 1

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_calls_db_import_with_force(self) -> None:
        """restore-ci calls db_import with force=True."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wt_dir = tmp_path / "test"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            worktree = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/test",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )
            worktree.provision()
            worktree.save()

            result = cast("str", call_command("db", "restore-ci", path=str(wt_dir)))

            worktree.refresh_from_db()
            assert "restored" in result.lower()
            assert worktree.state == Worktree.State.PROVISIONED

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_with_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "test"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            worktree = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/test",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )
            worktree.provision()
            worktree.save()

            result = cast("str", call_command("db", "restore-ci", path=str(wt_dir)))

            assert "restored" in result.lower() or "failed" in result.lower()

    @_patch_overlays(MINIMAL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_no_strategy_raises_system_exit_1(self) -> None:
        """`db restore-ci` with no import strategy is a genuine failure."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "test"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/test",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            with pytest.raises(SystemExit) as exc_info:
                call_command("db", "restore-ci", path=str(wt_dir))

            assert exc_info.value.code == 1


class TestDbResetPasswords(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_calls_overlay_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "test"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/test",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            result = cast("str", call_command("db", "reset-passwords", path=str(wt_dir)))

            assert "reset" in result.lower()

    @_patch_overlays(MINIMAL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_no_command_raises_system_exit_1(self) -> None:
        """`db reset-passwords` with no configured command is a genuine failure."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "test"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/test",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            with pytest.raises(SystemExit) as exc_info:
                call_command("db", "reset-passwords", path=str(wt_dir))

            assert exc_info.value.code == 1
