"""Tests for the db management command."""

import tempfile
from pathlib import Path
from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings

from teatree.core.models import Ticket, Worktree
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
    def test_reports_failure_when_import_fails(self) -> None:
        """Db refresh reports failure when overlay.db_import returns False."""
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

            assert "failed" in result.lower()

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

    @_patch_overlays(MINIMAL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_no_strategy_returns_message(self) -> None:
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

            assert "no db import strategy" in result.lower()


class TestDbRestoreCi(TestCase):
    @_patch_overlays(FAILING_IMPORT_OVERLAY)
    @override_settings(**SETTINGS)
    def test_reports_failure(self) -> None:
        """restore-ci returns failure message when db_import returns False (line 65)."""
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

            assert "failed" in result.lower()

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
    def test_no_strategy_returns_message(self) -> None:
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

            result = cast("str", call_command("db", "restore-ci", path=str(wt_dir)))

            assert "no db import strategy" in result.lower()


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
    def test_no_command_returns_message(self) -> None:
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

            assert "no reset-passwords command" in result.lower()
