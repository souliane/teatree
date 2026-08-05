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
    _check_no_session_scoped_worktrees,
    _check_one_worktree_root,
    _check_registered_worktrees_are_checkouts,
    check_worktree_health,
)
from teatree.core.models import Ticket, Worktree


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
        self.tmp = Path(tmp.name)

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
    def test_a_worktree_outside_the_canonical_root_warns(self) -> None:
        outside = self.tmp / "elsewhere" / "wt"
        outside.mkdir(parents=True)
        self._register(outside, branch="wt")
        canonical = mock.patch(
            "teatree.core.worktree.worktree_roots.canonical_worktree_root",
            return_value=self.tmp / "canonical",
        )
        canonical.start()
        self.addCleanup(canonical.stop)

        ok, out = _echoes(_check_one_worktree_root)

        # Advisory only — the split is NAMED but never reddens the run.
        assert ok is True
        assert "WARN" in out
        assert "outside the canonical root" in out

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


class SessionScopedWorktreeCheckTest(_TmpTestCase):
    """#4194: the registry knew the five stranded paths; no check read them."""

    def test_a_job_dir_worktree_is_named(self) -> None:
        stranded = self.tmp / ".claude" / "jobs" / "0e077e62" / "tmp" / "wt4101"
        stranded.mkdir(parents=True)
        self._register(stranded, branch="wt4101")

        ok, out = _echoes(_check_no_session_scoped_worktrees)

        # Advisory, like the split-root check — the point is that it is NAMED.
        assert ok is True
        assert "WARN" in out
        assert str(stranded) in out
        assert "session-scoped path" in out
        assert "workspace salvage" in out

    def test_a_harness_agent_worktree_is_named(self) -> None:
        agent_wt = self.tmp / ".claude" / "worktrees" / "agent-abc123"
        agent_wt.mkdir(parents=True)
        self._register(agent_wt, branch="agent-abc123")

        ok, out = _echoes(_check_no_session_scoped_worktrees)

        assert ok is True
        assert str(agent_wt) in out

    def test_a_durable_worktree_is_silent(self) -> None:
        durable = self.tmp / "teatree-worktrees" / "wt-4194"
        durable.mkdir(parents=True)
        self._register(durable, branch="wt-4194")

        ok, out = _echoes(_check_no_session_scoped_worktrees)

        assert ok is True
        assert out == ""


class WorktreeHealthAggregateTest(_TmpTestCase):
    def test_the_aggregate_names_a_session_scoped_worktree(self) -> None:
        stranded = self.tmp / ".claude" / "jobs" / "0e077e62" / "tmp" / "wt4101"
        stranded.mkdir(parents=True)
        (stranded / ".git").write_text("gitdir: /nonexistent/other/.git/worktrees/x\n", encoding="utf-8")
        self._register(stranded, branch="wt4101")

        _ok, out = _echoes(check_worktree_health)

        assert "session-scoped path" in out

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
