"""The ``check_worktree_health`` doctor probes (souliane/teatree#3583).

Functional: real ``Worktree`` rows point at real on-disk dirs (a broken checkout,
a checkout outside the canonical root), so the FAIL / WARN / degrade branches run
against the same registry the reaper reads.
"""

import io
import tempfile
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from django.test import TestCase

from teatree.cli.doctor.checks_worktree_health import (
    _check_occupied_checkouts,
    _check_one_worktree_root,
    _check_registered_worktrees_are_checkouts,
    _check_registered_worktrees_have_a_checkout,
    check_worktree_health,
)
from teatree.core.models import Ticket, Worktree
from teatree.core.worktree.occupancy import acquire
from tests._git_repo import make_git_repo, run_git


def _echoes(check: Callable[[], bool]) -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = check()
    return ok, buf.getvalue()


def _never_a_checkout(path: Path) -> Path:
    """A dir that EXISTS and carries no ``.git`` at all — the one provable dead shape."""
    path.mkdir(parents=True)
    (path / "leftover.txt").write_text("x", encoding="utf-8")
    return path


def _unresolvable_checkout(path: Path) -> Path:
    """A dir naming an admin dir this venue cannot reach — live elsewhere, or dead."""
    path.mkdir(parents=True)
    (path / ".git").write_text("gitdir: /nonexistent/other-context/.git/worktrees/gone\n", encoding="utf-8")
    return path


class _TmpTestCase(TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Git and the mount-point probe report physical paths on macOS, where
        # the lexical ``/var`` temp root resolves through ``/private/var``.
        self.tmp = Path(tmp.name).resolve()

    def _register(self, path: Path, *, branch: str) -> Worktree:
        ticket = Ticket.objects.create(issue_url=f"https://example.invalid/org/repo/issues/{branch}")
        return Worktree.objects.create(
            ticket=ticket,
            overlay="",
            repo_path="org/repo",
            branch=branch,
            extra={"worktree_path": str(path)},
        )


class RegisteredCheckoutCheckTest(_TmpTestCase):
    def test_a_dir_that_never_was_a_checkout_fails_and_is_named(self) -> None:
        dead = _never_a_checkout(self.tmp / "roots" / "dead-wt")
        self._register(dead, branch="dead-wt")

        ok, out = _echoes(_check_registered_worktrees_are_checkouts)

        assert ok is False
        assert "FAIL" in out
        assert "never was a git checkout" in out
        assert str(dead) in out

    def test_a_checkout_this_venue_cannot_resolve_warns_and_names_no_destructive_remedy(self) -> None:
        # #3912: a checkout created in another execution context is indistinguishable
        # from a dead one HERE. FAILing would print `release-dead-rows --apply` /
        # `clean-all` over live, in-flight work — the doctor instructing a data loss.
        unresolvable = _unresolvable_checkout(self.tmp / "roots" / "elsewhere")
        self._register(unresolvable, branch="elsewhere")

        ok, out = _echoes(_check_registered_worktrees_are_checkouts)

        assert ok is True
        assert "WARN" in out
        assert "UNVERIFIED" in out
        assert "does not exist in this execution context" in out
        assert "release-dead-rows" not in out
        assert "clean-all" not in out
        assert "workspace salvage" not in out

    def test_a_probe_git_declined_to_answer_warns_instead_of_failing(self) -> None:
        # FAILing here would print a remedy for a state no reaper is allowed to act
        # on — the doctor and the reaper share the probe precisely so they agree.
        self._register(_never_a_checkout(self.tmp / "roots" / "unanswerable"), branch="unanswerable")
        refusal = mock.Mock(returncode=128, stdout="", stderr="fatal: detected dubious ownership in repository")

        with mock.patch("teatree.core.worktree.worktree_roots.run_allowed_to_fail", return_value=refusal):
            ok, out = _echoes(_check_registered_worktrees_are_checkouts)

        assert ok is True
        assert "WARN" in out
        assert "UNVERIFIED" in out

    def test_a_missing_dir_is_not_a_failure(self) -> None:
        # An absent dir is an ordinary reaped worktree, not the broken-checkout state.
        self._register(self.tmp / "roots" / "gone", branch="gone")
        ok, out = _echoes(_check_registered_worktrees_are_checkouts)
        assert ok is True
        assert out == ""


class OneWorktreeRootCheckTest(_TmpTestCase):
    """The split-namespace WARN, against real checkouts relocate can and cannot move.

    Real git under ``tmp_path``: relocate's refusal policy runs for real, so the
    partition this asserts is the one an operator would actually get.
    """

    def _pin_canonical(self) -> Path:
        canonical_root = self.tmp / "canonical"
        canonical = mock.patch(
            "teatree.core.worktree.worktree_roots.canonical_worktree_root",
            return_value=canonical_root,
        )
        canonical.start()
        self.addCleanup(canonical.stop)
        return canonical_root

    def _checkout_outside(self, branch: str) -> Path:
        """A real linked worktree under ``<tmp>/elsewhere`` — movable unless made otherwise."""
        clone = make_git_repo(self.tmp / "elsewhere" / "myrepo")
        checkout = self.tmp / "elsewhere" / branch / "myrepo"
        run_git(clone, "worktree", "add", "-q", "-b", branch, str(checkout))
        self._register(checkout, branch=branch)
        return checkout

    def test_a_relocatable_worktree_outside_the_canonical_root_warns(self) -> None:
        self._checkout_outside("wt")
        self._pin_canonical()

        ok, out = _echoes(_check_one_worktree_root)

        # Advisory only — the split is NAMED but never reddens the run.
        assert ok is True
        assert "WARN" in out
        assert "outside the canonical root" in out
        assert "Fix: t3 <overlay> workspace relocate." in out

    def test_a_worktree_relocate_refuses_is_named_and_gets_no_relocate_remedy(self) -> None:
        # #4368: counting a row relocate refuses forever prescribes a command that
        # cannot discharge the finding, so the WARN recurs at that number for good.
        checkout = self._checkout_outside("wt")
        (checkout / "scratch.txt").write_text("uncommitted", encoding="utf-8")
        self._pin_canonical()

        ok, out = _echoes(_check_one_worktree_root)

        assert ok is True
        assert "WARN" in out
        assert "outside the canonical root" in out
        assert f"{checkout}: uncommitted changes" in out
        assert "workspace relocate" not in out

    def test_a_mixed_set_counts_only_the_relocatable_ones_in_the_remedy(self) -> None:
        self._checkout_outside("movable")
        stuck = self._checkout_outside("stuck")
        (stuck / "scratch.txt").write_text("uncommitted", encoding="utf-8")
        canonical_root = self._pin_canonical()

        ok, out = _echoes(_check_one_worktree_root)

        assert ok is True
        assert "1 of 2 registered worktree(s)" in out
        assert "Fix: t3 <overlay> workspace relocate." in out
        assert f"1 registered worktree(s) live outside the canonical root {canonical_root} that" in out
        assert f"{stuck}: uncommitted changes" in out

    def test_a_cross_mount_worktree_is_named_by_its_boundary_not_prescribed_relocate(self) -> None:
        # The deployment shape of #4368: the checkout and the canonical root are
        # separate bind mounts of ONE device, so `git worktree move` returns EXDEV.
        checkout = self._checkout_outside("wt")
        canonical_root = self._pin_canonical()
        canonical_root.mkdir(parents=True)
        rows = [
            f"36 28 252:0 / {point} rw,relatime - ext4 /dev/mapper/hk-root rw"
            for point in (Path("/"), self.tmp / "elsewhere", canonical_root)
        ]
        mountinfo = self.tmp / "mountinfo"
        mountinfo.write_text("\n".join(rows) + "\n", encoding="utf-8")

        with mock.patch("teatree.utils.mount_points._MOUNTINFO", mountinfo):
            ok, out = _echoes(_check_one_worktree_root)

        assert ok is True
        assert f"{checkout}: mount-point boundary" in out
        assert "EXDEV" in out
        assert "workspace relocate" not in out

    def test_a_half_moved_row_is_counted_movable_not_refused(self) -> None:
        # #4368 regression: `run_relocate` HEALS a row whose recorded path is gone
        # from disk but whose target already sits under the canonical root as a
        # git worktree (a prior move succeeded, only the DB save threw) — see
        # `_workspace.relocate._reconcile_half_move`. Naming this row "missing on
        # disk (stale row)" and withholding the `workspace relocate` remedy would
        # repeat #4368's own bug class: a finding relocate CAN discharge, reported
        # as one it cannot.
        checkout = self._checkout_outside("wt")
        clone = self.tmp / "elsewhere" / "myrepo"
        canonical_root = self._pin_canonical()
        target = canonical_root / "wt" / "myrepo"
        target.parent.mkdir(parents=True)
        run_git(clone, "worktree", "move", str(checkout), str(target))
        # The row still records the OLD (now-gone) path — a half-move, not a fresh row.

        ok, out = _echoes(_check_one_worktree_root)

        assert ok is True
        assert "1 of 1 registered worktree(s)" in out
        assert "Fix: t3 <overlay> workspace relocate." in out
        assert "stale row" not in out
        assert "missing on disk" not in out

    def test_all_worktrees_inside_the_canonical_root_is_silent(self) -> None:
        inside = self.tmp / "canonical" / "wt"
        inside.mkdir(parents=True)
        self._register(inside, branch="wt")
        canonical = mock.patch(
            "teatree.core.worktree.worktree_roots.canonical_worktree_root",
            return_value=self.tmp / "canonical",
        )
        canonical.start()
        self.addCleanup(canonical.stop)

        ok, out = _echoes(_check_one_worktree_root)

        assert ok is True
        assert out == ""


class OccupiedCheckoutCheckTest(_TmpTestCase):
    """#3952: who holds a checkout is reported, and holding one is never a failure."""

    def test_an_unheld_registry_is_silent(self) -> None:
        self._register(self.tmp / "roots" / "free", branch="free")

        ok, out = _echoes(_check_occupied_checkouts)

        assert ok is True
        assert out == ""

    def test_a_held_checkout_is_reported_as_info_with_its_holder(self) -> None:
        held = self._register(self.tmp / "roots" / "busy", branch="busy")
        acquire(held, holder="task:7", holder_session="session-A")

        ok, out = _echoes(_check_occupied_checkouts)

        assert ok is True
        assert "INFO" in out
        assert "task:7" in out
        assert "session-A" in out
        assert "release-occupancy" in out


class WorktreeHealthAggregateTest(_TmpTestCase):
    def test_an_unreadable_registry_degrades_to_unverified_pass(self) -> None:
        boom = mock.patch(
            "teatree.cli.doctor.checks_worktree_health._check_registered_worktrees_are_checkouts",
            side_effect=RuntimeError("db down"),
        )
        boom.start()
        self.addCleanup(boom.stop)

        ok, out = _echoes(check_worktree_health)

        assert ok is True
        assert "UNVERIFIED" in out


class VanishedCheckoutCheckTest(_TmpTestCase):
    """A row whose directory is GONE was reported by no doctor surface at all.

    The two checks above filter to rows whose dir EXISTS, which is right for the
    verdicts they render — absence proves nothing one venue may act on. It also
    left the biggest population of the registry invisible: measured on a live box,
    114 of 137 registered rows pointed at a directory that was not there, and the
    doctor said nothing about any of them. An inventory that is 83% stale silently
    is worse than one that is loud about it.

    So this reports, and reports only. The one remedy it names is the read-only
    `release-dead-rows` disposition report, which keeps every such row. What it
    adds is the count, and the split between a checkout this venue could have seen
    and one whose whole neighbourhood is unmounted here — the distinction that
    made a container-only checkout read as lost work.
    """

    def _vanished(self, name: str) -> Path:
        """A path under a readable parent that does not exist — genuinely absent HERE."""
        parent = self.tmp / "roots"
        parent.mkdir(parents=True, exist_ok=True)
        return parent / name

    def _pin_canonical(self, root: Path) -> None:
        self.enterContext(
            mock.patch(
                "teatree.core.worktree.worktree_roots.canonical_worktree_root",
                return_value=root,
            )
        )

    def test_a_row_whose_directory_is_gone_is_reported(self) -> None:
        self._register(self._vanished("gone-wt"), branch="gone-wt")

        ok, out = _echoes(_check_registered_worktrees_have_a_checkout)

        assert ok, "absence proves nothing, so this reports without failing the run"
        assert "gone-wt" in out
        assert "WARN" in out

    def test_a_live_checkout_is_not_reported(self) -> None:
        live = make_git_repo(self.tmp / "live-wt")
        run_git(live, "checkout", "-b", "live-wt")
        self._register(live, branch="live-wt")

        ok, out = _echoes(_check_registered_worktrees_have_a_checkout)

        assert ok
        assert out.strip() == "", f"a live checkout is not drift: {out}"

    def test_each_row_is_named_only_in_its_own_venue_verdict(self) -> None:
        canonical = self.tmp / "canonical"
        canonical.mkdir()
        self._register(canonical / "deleted-ticket" / "repo", branch="gone-wt")
        self._register(self.tmp / "unmounted-root" / "other-context" / "wt", branch="elsewhere-wt")
        self._pin_canonical(canonical)

        _ok, out = _echoes(_check_registered_worktrees_have_a_checkout)

        absent_line = next(line for line in out.splitlines() if "READ as absent" in line)
        unknown_line = next(line for line in out.splitlines() if "UNKNOWN here" in line)
        assert "gone-wt" in absent_line
        assert "elsewhere-wt" not in absent_line
        assert "elsewhere-wt" in unknown_line
        assert "gone-wt" not in unknown_line

    def test_a_path_that_exists_as_a_file_is_not_reported_as_absent(self) -> None:
        wrong_kind = self.tmp / "not-a-directory"
        wrong_kind.write_text("contents", encoding="utf-8")
        self._register(wrong_kind, branch="wrong-kind")

        _ok, out = _echoes(_check_registered_worktrees_have_a_checkout)

        invalid_line = next(line for line in out.splitlines() if "is not a directory" in line)
        assert "wrong-kind" in invalid_line
        assert "READ as absent" not in out
        assert "UNKNOWN here" not in out

    def test_it_names_only_the_read_only_disposition_command(self) -> None:
        self._register(self._vanished("gone-wt"), branch="gone-wt")

        _ok, out = _echoes(_check_registered_worktrees_have_a_checkout)

        assert "release-dead-rows" in out
        assert "salvage" not in out
        assert "--apply" not in out, "no remedy here may delete: absence is not proof of deadness"

    def test_an_unreadable_registry_does_not_crash_the_run(self) -> None:
        with mock.patch(
            "teatree.core.models.Worktree.objects.all",
            side_effect=RuntimeError("registry unreadable"),
        ):
            ok, out = _echoes(check_worktree_health)

        assert ok
        assert "UNVERIFIED" in out
