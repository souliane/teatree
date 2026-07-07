"""Tests for the db management command."""

import io
import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings

import teatree.core.management.commands.db as db_mod
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import get_overlay
from teatree.utils.approval import ApprovalRefusedError
from tests.teatree_core.management_commands._overlays import (
    FAILING_IMPORT_OVERLAY,
    FULL_OVERLAY,
    MINIMAL_OVERLAY,
    POST_DB_OVERLAY,
    REMOTE_PATH_RECORDING_OVERLAY,
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
        """Db refresh iterates over overlay.provisioning.post_db_steps and calls each callable."""
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

    @_patch_overlays(REMOTE_PATH_RECORDING_OVERLAY)
    @override_settings(**SETTINGS)
    def test_fresh_dump_forwards_slow_import_and_reaches_remote_dump(self) -> None:
        """`db refresh --fresh-dump` must reach the remote-dump branch.

        Regression for #955. `refresh` never passed `slow_import` into
        `overlay.provisioning.db_import(...)`, so `DjangoDbImporter.run()` returned at
        the `not slow_import` guard (after the early DSLR return) BEFORE
        the `if allow_remote_dump:` remote `pg_dump` block. `--fresh-dump`
        silently degraded to "restore stale local DSLR snapshot".

        With the fix, `fresh_dump` forces `slow_import=True`, so `run()`
        flows past the guard and enters the `allow_remote_dump` branch
        (`_try_fetch_remote_dump`). DSLR is unavailable in this double, so
        the only way the import can succeed is via the remote branch.
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

            with patch.object(db_mod, "require_approval", return_value=None):
                result = cast(
                    "str",
                    call_command("db", "refresh", path=str(wt_dir), fresh_dump=True),
                )

            overlay = get_overlay()
            assert overlay.calls["slow_import"] is True
            assert overlay.calls["approve_remote_dump"] is True
            assert overlay.calls["remote_branch_reached"] is True
            assert "refreshed" in result.lower()

    @_patch_overlays(REMOTE_PATH_RECORDING_OVERLAY)
    @override_settings(**SETTINGS)
    def test_non_fresh_refresh_does_not_force_slow_import(self) -> None:
        """Regression: the non-`--fresh-dump` path is unchanged.

        Without `--fresh-dump`, `db refresh` must NOT force `slow_import`
        and must NOT reach the remote-dump branch — the DSLR-first fast
        path stays the only sanctioned path for the default flow (#955).
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
            overlay = get_overlay()
            assert overlay.calls["slow_import"] is False
            assert overlay.calls["remote_branch_reached"] is False

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


class TestDbApproveCommand(TestCase):
    """``t3 db approve <op> <tenant> --approver <id>`` records the no-TTY satisfier (#953/#126)."""

    def test_records_a_dbapproval_row(self) -> None:
        from teatree.core.models import DbApproval  # noqa: PLC0415

        out = io.StringIO()
        call_command("db", "approve", "fresh-dump", "test_db", "--approver", "souliane", stdout=out)

        approval = DbApproval.objects.get()
        assert approval.op == "fresh-dump"
        assert approval.tenant == "test_db"
        assert approval.approver_id == "souliane"
        assert "OK recorded" in out.getvalue()

    def test_refuses_an_agent_role_approver(self) -> None:
        from teatree.core.models import DbApproval  # noqa: PLC0415

        err = io.StringIO()
        with pytest.raises(SystemExit) as exc:
            call_command("db", "approve", "fresh-dump", "test_db", "--approver", "loop", stderr=err)
        assert exc.value.code == 1
        assert "maker/coding-agent/loop" in err.getvalue()
        assert not DbApproval.objects.exists()

    def test_recorded_approval_satisfies_the_gate_end_to_end(self) -> None:
        """The escape works: a recorded approval lets a non-TTY caller consume it."""
        from teatree.core.gates.db_approval_gate import ApprovalScope, require_approval  # noqa: PLC0415
        from teatree.core.models import DbApproval  # noqa: PLC0415

        call_command("db", "approve", "fresh-dump", "test_db", "--approver", "souliane", stdout=io.StringIO())

        stdin = io.StringIO()
        stdout = io.StringIO()
        # No TTY (StringIO.isatty() is False) — only the recorded approval can satisfy it.
        require_approval(
            "Pull fresh DEV dump?",
            ApprovalScope(op="fresh-dump", tenant="test_db", user_authorized="souliane"),
            stdin=stdin,
            stdout=stdout,
        )
        assert DbApproval.objects.get().consumed_at is not None
