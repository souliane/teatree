"""Public settlement pass over held author tickets."""

import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Task, Ticket, Worktree
from teatree.core.models.ticket_worktree_checks import WorktreeProbeUnverifiableError
from teatree.loop.shipping_hold_evidence import Evidence, disk_evidence
from teatree.loop.shipping_hold_settle import Settlement, settle_held_tickets


class _Host:
    def __init__(self, branches: list[str] | None = None) -> None:
        self.branches = branches or []
        self.calls = 0

    def list_unshipped_branches(self, *, repo: str, prefix: str) -> list[str]:
        self.calls += 1
        return [name for name in self.branches if name.startswith(prefix)]


def _git(path: Path, *args: str) -> None:
    executable = shutil.which("git")
    assert executable is not None
    subprocess.run([executable, "-C", str(path), *args], check=True, capture_output=True)


class TestShippingHoldSettlement(TestCase):
    def _ticket(self, number: int = 5346) -> Ticket:
        now = timezone.now()
        return Ticket.objects.create(
            overlay="test",
            role=Ticket.Role.AUTHOR,
            state=Ticket.State.SELF_REVIEWED,
            issue_url=f"https://github.com/souliane/teatree/issues/{number}",
            extra={
                "shipping_hold": {
                    "reason": "no worktree row",
                    "skipped": "no shippable diff",
                    "held_at": now.isoformat(),
                    "checked_at": "",
                }
            },
        )

    def _run(self, root: Path, host: _Host, *, now: datetime | None = None, dry_run: bool = False) -> list[Settlement]:
        with (
            patch("teatree.loop.shipping_hold_evidence.worktree_root", return_value=root),
            patch("teatree.loop.shipping_hold_evidence.get_overlay", return_value=object()),
            patch("teatree.loop.shipping_hold_evidence.get_code_host_for_url", return_value=host),
        ):
            return settle_held_tickets(now=now, dry_run=dry_run)

    def test_rowless_empty_sources_stay_held(self) -> None:
        ticket = self._ticket()
        with TemporaryDirectory() as directory:
            results = self._run(Path(directory), _Host())
        ticket.refresh_from_db()
        assert results[0].action == "hold"
        assert ticket.extra["shipping_hold"]["reason"] == "no worktree row; nothing on the workspaces root or the forge"
        assert ticket.extra["shipping_hold"]["checked_at"]
        assert ticket.state == Ticket.State.SELF_REVIEWED

    def test_rowless_dry_run_reports_hold(self) -> None:
        ticket = self._ticket()
        with TemporaryDirectory() as directory:
            results = self._run(Path(directory), _Host(), dry_run=True)
        assert results == [
            Settlement(ticket.pk, "hold", "no worktree row; nothing on the workspaces root or the forge")
        ]
        ticket.refresh_from_db()
        assert ticket.extra["shipping_hold"]["checked_at"] == ""

    def test_row_with_verified_empty_sources_is_ignored(self) -> None:
        ticket = self._ticket()
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="souliane/teatree", branch="5346-work")
        with TemporaryDirectory() as directory:
            results = self._run(Path(directory), _Host())
        ticket.refresh_from_db()
        assert results[0].action == "ignore"
        assert ticket.state == Ticket.State.IGNORED
        assert ticket.extra["shipping_skipped"] == "no shippable diff"
        assert "shipping_hold" not in ticket.extra
        assert ticket.extra["ignored_from"] == Ticket.State.SELF_REVIEWED

    def test_row_branch_outside_issue_prefix_is_scanned_on_forge(self) -> None:
        ticket = self._ticket()
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="souliane/teatree", branch="custom-work")
        with TemporaryDirectory() as directory:
            results = self._run(Path(directory), _Host(["custom-work"]))
        ticket.refresh_from_db()
        assert results[0].action == "hold"
        assert results[0].reason == "branch custom-work"
        assert ticket.state == Ticket.State.SELF_REVIEWED

    def test_other_recorded_repo_branch_keeps_ticket_held(self) -> None:
        ticket = self._ticket()
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="souliane/other-repo", branch="5346-work")

        def branches(*, repo: str, prefix: str) -> list[str]:
            return ["5346-work"] if repo == "souliane/other-repo" and prefix == "5346-" else []

        with (
            TemporaryDirectory() as directory,
            patch("teatree.loop.shipping_hold_evidence.get_overlay", return_value=object()),
            patch("teatree.loop.shipping_hold_evidence.get_code_host_for_url") as backend,
            patch("teatree.loop.shipping_hold_evidence.worktree_root", return_value=Path(directory)),
        ):
            backend.return_value.list_unshipped_branches.side_effect = branches
            results = settle_held_tickets()
        assert results[0].action == "hold"
        assert results[0].reason == "branch 5346-work"
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SELF_REVIEWED

    def test_other_recorded_repo_checkout_is_evidence(self) -> None:
        ticket = self._ticket()
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="souliane/other-repo", branch="5346-work")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "5346-work" / "other-repo"
            path.mkdir(parents=True)
            _git(path, "init", "-b", "main")
            _git(
                path,
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Test",
                "commit",
                "--allow-empty",
                "-m",
                "base",
            )
            _git(path, "checkout", "-b", "5346-work")
            _git(
                path,
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Test",
                "commit",
                "--allow-empty",
                "-m",
                "work",
            )
            _git(path, "remote", "add", "origin", "https://github.com/souliane/other-repo.git")
            with patch("teatree.loop.shipping_hold_evidence.worktree_root", return_value=root):
                assert str(path) in disk_evidence(ticket).found

    def test_unreadable_recorded_checkout_stays_held(self) -> None:
        ticket = self._ticket()
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="souliane/teatree", branch="5346-work")
        with (
            patch(
                "teatree.loop.shipping_hold_settle.worktree_has_commits_ahead",
                side_effect=WorktreeProbeUnverifiableError("bad ref"),
            ),
            patch("teatree.loop.shipping_hold_settle.disk_evidence", return_value=Evidence()),
            patch("teatree.loop.shipping_hold_settle.forge_evidence", return_value=Evidence()),
        ):
            results = settle_held_tickets()
        assert results[0].action == "hold"
        assert results[0].reason == "unverified: git (bad ref)"

    def test_failed_candidates_back_off_so_later_tickets_are_reached(self) -> None:
        tickets = [self._ticket(number=5400 + index) for index in range(11)]
        failed_ids = {ticket.pk for ticket in tickets[:10]}
        now = timezone.now()

        def read_disk(ticket: Ticket) -> Evidence:
            if ticket.pk in failed_ids:
                message = "disk unavailable"
                raise RuntimeError(message)
            return Evidence()

        with (
            patch("teatree.loop.shipping_hold_settle.disk_evidence", side_effect=read_disk),
            patch("teatree.loop.shipping_hold_settle.forge_evidence", return_value=Evidence()),
        ):
            with pytest.raises(RuntimeError, match="shipping-hold settlement failed"):
                settle_held_tickets(now=now)
            results = settle_held_tickets(now=now)
        assert [result.ticket_pk for result in results] == [tickets[-1].pk]
        assert all(Ticket.objects.get(pk=ticket.pk).extra["shipping_hold"]["checked_at"] for ticket in tickets[:10])

    def test_partial_applied_results_survive_a_failed_ticket(self) -> None:
        failing = self._ticket(number=5410)
        applied = self._ticket(number=5411)

        def read_disk(ticket: Ticket) -> Evidence:
            if ticket.pk == failing.pk:
                message = "disk unavailable"
                raise RuntimeError(message)
            return Evidence()

        with (
            patch("teatree.loop.shipping_hold_settle.disk_evidence", side_effect=read_disk),
            patch("teatree.loop.shipping_hold_settle.forge_evidence", return_value=Evidence()),
            pytest.raises(RuntimeError) as error,
        ):
            settle_held_tickets()
        assert [result.ticket_pk for result in error.value.applied] == [applied.pk]

    def test_row_removed_before_ticket_lock_stays_held(self) -> None:
        ticket = self._ticket()
        row = Worktree.objects.create(ticket=ticket, overlay="test", repo_path="souliane/teatree", branch="5346-work")
        host = _Host()

        def remove_row(*, repo: str, prefix: str) -> list[str]:
            row.delete()
            return []

        with TemporaryDirectory() as directory, patch.object(host, "list_unshipped_branches", side_effect=remove_row):
            results = self._run(Path(directory), host)
        ticket.refresh_from_db()
        assert results[0].action == "hold"
        assert ticket.state == Ticket.State.SELF_REVIEWED
        assert ticket.extra["shipping_hold"]["reason"] == "no worktree row; nothing on the workspaces root or the forge"
        assert "shipping_skipped" not in ticket.extra

    def test_row_branch_changed_after_scan_stays_held(self) -> None:
        ticket = self._ticket()
        row = Worktree.objects.create(ticket=ticket, overlay="test", repo_path="souliane/teatree", branch="5346-empty")
        host = _Host()

        def change_branch(*, repo: str, prefix: str) -> list[str]:
            row.branch = "5346-new-work"
            row.save(update_fields=["branch"])
            return []

        with (
            TemporaryDirectory() as directory,
            patch.object(host, "list_unshipped_branches", side_effect=change_branch),
        ):
            results = self._run(Path(directory), host)
        ticket.refresh_from_db()
        assert results[0].action == "hold"
        assert ticket.state == Ticket.State.SELF_REVIEWED

    def test_recorded_branch_changed_during_forge_scan_stays_held(self) -> None:
        ticket = self._ticket()
        ticket.merge_extra(set_keys={"branch": "old-branch"})
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="souliane/teatree", branch="5346-empty")
        host = _Host(["late-work"])

        def change_recorded_branch(*, repo: str, prefix: str) -> list[str]:
            Ticket.objects.get(pk=ticket.pk).merge_extra(set_keys={"branch": "late-work"})
            return []

        with (
            TemporaryDirectory() as directory,
            patch.object(host, "list_unshipped_branches", side_effect=change_recorded_branch),
        ):
            results = self._run(Path(directory), host)
        ticket.refresh_from_db()
        assert results[0].action == "hold"
        assert ticket.state == Ticket.State.SELF_REVIEWED
        assert ticket.extra["branch"] == "late-work"

        with TemporaryDirectory() as directory:
            followup = self._run(Path(directory), host, now=timezone.now() + timedelta(minutes=16))
        assert followup[0].action == "hold"
        assert followup[0].reason == "branch late-work"

    def test_row_checkout_path_changed_after_scan_stays_held(self) -> None:
        ticket = self._ticket()
        row = Worktree.objects.create(ticket=ticket, overlay="test", repo_path="souliane/teatree", branch="5346-empty")
        host = _Host()

        def change_checkout(*, repo: str, prefix: str) -> list[str]:
            row.extra = {"worktree_path": "/tmp/5346-new-checkout"}
            row.save(update_fields=["extra"])
            return []

        with (
            TemporaryDirectory() as directory,
            patch.object(host, "list_unshipped_branches", side_effect=change_checkout),
        ):
            results = self._run(Path(directory), host)
        ticket.refresh_from_db()
        assert results[0].action == "hold"
        assert ticket.state == Ticket.State.SELF_REVIEWED

    def test_row_added_after_evidence_scan_is_rechecked_before_ignore(self) -> None:
        ticket = self._ticket()
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="souliane/teatree", branch="5346-empty")
        host = _Host()
        with TemporaryDirectory() as directory, TemporaryDirectory() as checkout_dir:
            root = Path(directory)
            path = Path(checkout_dir) / "new-checkout"
            path.mkdir()
            _git(path, "init", "-b", "main")
            _git(
                path,
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Test",
                "commit",
                "--allow-empty",
                "-m",
                "base",
            )
            _git(path, "checkout", "-b", "5346-work")
            _git(
                path,
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Test",
                "commit",
                "--allow-empty",
                "-m",
                "work",
            )

            def add_row(*, repo: str, prefix: str) -> list[str]:
                Worktree.objects.create(
                    ticket=ticket,
                    overlay="test",
                    repo_path="souliane/teatree",
                    branch="5346-work",
                    extra={"worktree_path": str(path)},
                )
                return []

            with patch.object(host, "list_unshipped_branches", side_effect=add_row):
                results = self._run(root, host)
            ticket.refresh_from_db()
            assert results[0].action == "hold"
            assert ticket.state == Ticket.State.SELF_REVIEWED
            assert "shipping_skipped" not in ticket.extra
            assert self._run(root, _Host(), now=timezone.now() + timedelta(minutes=16))[0].action == "release"
        assert Task.objects.filter(ticket=ticket, phase="shipping").count() == 1

    def test_unreadable_forge_with_a_row_is_held_as_unverified_never_ignored(self) -> None:
        ticket = self._ticket()
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="souliane/teatree", branch="5346-work")
        host = _Host()
        with (
            TemporaryDirectory() as directory,
            patch.object(host, "list_unshipped_branches", side_effect=RuntimeError("HTTP 500")),
        ):
            results = self._run(Path(directory), host)
        ticket.refresh_from_db()
        assert results[0].action == "hold"
        assert ticket.state == Ticket.State.SELF_REVIEWED
        assert ticket.extra["shipping_hold"]["reason"] == "unverified: forge (RuntimeError: HTTP 500)"

    def test_dangling_git_metadata_with_empty_forge_stays_held(self) -> None:
        ticket = self._ticket()
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="souliane/teatree", branch="5346-empty")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "5346-ticket"
            checkout.mkdir()
            (checkout / ".git").symlink_to("missing-gitdir")
            results = self._run(root, _Host())
        ticket.refresh_from_db()
        assert results[0].action == "hold"
        assert ticket.state == Ticket.State.SELF_REVIEWED
        assert ticket.extra["shipping_hold"]["reason"].startswith("unverified: git (")

    def test_pass_settles_the_ten_least_recently_checked_tickets(self) -> None:
        now = timezone.now()
        tickets = [self._ticket(7000 + index) for index in range(11)]
        for age, ticket in enumerate(tickets):
            stamp = (now - timedelta(hours=age + 1)).isoformat()
            ticket.merge_extra(set_keys={"shipping_hold": {**ticket.extra["shipping_hold"], "checked_at": stamp}})
        with TemporaryDirectory() as directory:
            results = self._run(Path(directory), _Host(), now=now)
        assert len(results) == 10
        newest_checked = tickets[0]
        newest_checked.refresh_from_db()
        assert newest_checked.extra["shipping_hold"]["reason"] == "no worktree row"
        assert all(
            Ticket.objects.get(pk=t.pk).extra["shipping_hold"]["checked_at"] == now.isoformat() for t in tickets[1:]
        )

    def test_unmerged_forge_branch_keeps_hold(self) -> None:
        ticket = self._ticket()
        with TemporaryDirectory() as directory:
            results = self._run(Path(directory), _Host(["5346-work"]))
        ticket.refresh_from_db()
        assert results[0].action == "hold"
        assert ticket.extra["shipping_hold"]["reason"] == "branch 5346-work"

    def test_recorded_checkout_commits_release_once(self) -> None:
        ticket = self._ticket()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "checkout"
            path.mkdir()
            _git(path, "init", "-b", "main")
            _git(
                path,
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Test",
                "commit",
                "--allow-empty",
                "-m",
                "base",
            )
            _git(path, "checkout", "-b", "5346-work")
            _git(
                path,
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Test",
                "commit",
                "--allow-empty",
                "-m",
                "work",
            )
            Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="souliane/teatree",
                branch="5346-work",
                extra={"worktree_path": str(path)},
            )
            assert self._run(Path(directory), _Host())[0].action == "release"
            assert self._run(Path(directory), _Host()) == []
        ticket.refresh_from_db()
        assert "shipping_hold" not in ticket.extra
        assert Task.objects.filter(ticket=ticket, phase="shipping").count() == 1

    def test_recheck_window_and_dry_run_do_not_write(self) -> None:
        ticket = self._ticket()
        host = _Host()
        now = timezone.now()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            assert self._run(root, host, now=now, dry_run=True)[0].action == "hold"
            ticket.refresh_from_db()
            assert ticket.extra["shipping_hold"]["checked_at"] == ""
            self._run(root, host, now=now)
            assert self._run(root, host, now=now + timedelta(minutes=14)) == []
            assert host.calls == 2
            self._run(root, host, now=now + timedelta(minutes=16))
            assert host.calls == 3

    def test_scheduling_failure_rolls_back_hold_removal(self) -> None:
        ticket = self._ticket()
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="souliane/teatree", branch="5346-work")
        with (
            patch("teatree.loop.shipping_hold_settle.worktree_has_commits_ahead", return_value=True),
            patch.object(Ticket, "schedule_shipping", side_effect=RuntimeError("scheduler unavailable")),
            pytest.raises(RuntimeError, match="scheduler unavailable"),
        ):
            settle_held_tickets()
        ticket.refresh_from_db()
        assert ticket.extra["shipping_hold"]["reason"] == (
            "unverified: settlement failed (RuntimeError: scheduler unavailable)"
        )
        assert not Task.objects.filter(ticket=ticket, phase="shipping").exists()

    def test_claim_refusal_writes_nothing(self) -> None:
        ticket = self._ticket()
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="souliane/teatree", branch="5346-work")
        with (
            patch("teatree.loop.shipping_hold_settle.worktree_has_commits_ahead", return_value=True),
            patch("teatree.core.managers_task_claim.claim_admission_block_reason", return_value="quiescing"),
        ):
            assert settle_held_tickets() == []
        ticket.refresh_from_db()
        assert ticket.extra["shipping_hold"]["checked_at"] == ""
        assert not Task.objects.filter(ticket=ticket, phase="shipping").exists()

    def test_moved_ticket_is_not_settled_from_stale_forge_evidence(self) -> None:
        ticket = self._ticket()
        host = _Host()

        def move_ticket(*, repo: str, prefix: str) -> list[str]:
            Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.MERGED)
            return []

        with TemporaryDirectory() as directory, patch.object(host, "list_unshipped_branches", side_effect=move_ticket):
            assert self._run(Path(directory), host) == []
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert "shipping_hold" in ticket.extra
