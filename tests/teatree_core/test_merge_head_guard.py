"""The keystone merge must never leave the caller's clone on a detached HEAD (#2383).

The §17.4 keystone runs from inside the primary clone. The cross-repo
SHA-recovery probe (or any future local tree read) can ``git checkout`` a branch
in the cwd repo; left unrestored, the clone detaches at the merged PR branch tip
and the next ``git pull --ff-only origin/main`` aborts with "Not possible to
fast-forward". These tests drive the REAL :func:`merge_ticket_pr` against a real
``tmp_path`` clone checked out on ``main``, make the merge path perform a real
local checkout (modeling the probe fallback the ticket names), and assert the
clone is STILL on ``main`` afterward. Only the unstoppable external — the ``gh``
merge subprocess — is stubbed; git itself is real.

Anti-vacuity (see ``TestRestoreCallerBranchPrimitive``): the guard's restore is
exercised against a genuine local checkout, so removing the restore turns the
assertions RED — the merge path with the guard bypassed leaves the clone
detached, exactly the #2383 symptom.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.merge import merge_ticket_pr, restore_caller_branch
from teatree.core.merge.head_guard import _capture_head, _restore_head
from teatree.core.models import MergeClear, Ticket

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "a" * 40
_GREEN = '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]'
_PR_BRANCH = "2369-publish-gate-body-resolution"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _make_clone(tmp_path: Path) -> Path:
    """A real git clone on ``main`` with a separate PR branch and an ``origin`` remote.

    Mirrors the primary-clone shape the keystone runs from: ``main`` checked
    out, a ``refs/remotes/origin/<pr-branch>`` the probe fallback would
    ``git checkout`` (detaching HEAD), and ``main`` left intact so a restore
    has somewhere to return to.
    """
    upstream = tmp_path / "upstream.git"
    _git(tmp_path, "init", "--bare", str(upstream))

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(upstream), str(clone))
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    _git(clone, "checkout", "-b", "main")
    (clone / "f.txt").write_text("base\n", encoding="utf-8")
    _git(clone, "add", "f.txt")
    _git(clone, "commit", "-m", "base")
    _git(clone, "push", "-u", "origin", "main")

    _git(clone, "checkout", "-b", _PR_BRANCH)
    (clone / "f.txt").write_text("pr work\n", encoding="utf-8")
    _git(clone, "commit", "-am", "pr work")
    _git(clone, "push", "-u", "origin", _PR_BRANCH)

    _git(clone, "checkout", "main")
    _git(clone, "fetch", "origin")
    return clone


def _ticket() -> Ticket:
    return Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)


def _clear(ticket: Ticket) -> MergeClear:
    return MergeClear.objects.create(
        ticket=ticket,
        pr_id=2380,
        slug="souliane/teatree",
        reviewed_sha=_SHA,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.DOCS,
    )


def _gh_ok(argv: list[str]) -> tuple[int, str, str]:
    joined = " ".join(argv)
    if "headRefOid" in joined:
        return (0, _SHA, "")
    if "isDraft" in joined:
        return (0, "false", "")
    if "statusCheckRollup" in joined:
        return (0, _GREEN, "")
    if "state,mergeCommit" in joined:
        return (0, '{"state": "OPEN", "mergeCommit": null}', "")
    if "pulls" in joined and "merge" in joined:
        return (0, '{"sha": "merged0deadbeef"}', "")
    return (0, "", "")


class TestMergeKeystoneRestoresCallerBranch(TestCase):
    """#2383: the keystone leaves the cwd clone on the branch it was on."""

    def _run_with_probe_checkout(self, clone: Path) -> object:
        """Drive the real ``merge_ticket_pr``; the slug probe does a real local checkout.

        ``_reconcile_slug_against_reviewed_sha`` is the cross-repo SHA-recovery
        probe the ticket names. Its production body is API-only, but the
        fallback the ticket worries about reads a PR's tree via a local
        ``git checkout origin/<pr-branch>``. The patch models EXACTLY that — a
        real checkout that detaches HEAD — so the head guard's restore is what
        keeps the clone on ``main``. The patch returns the original slug so the
        rest of the keystone proceeds unchanged.
        """

        def _probe_does_local_checkout(*, initial_slug: str, **_kwargs: object) -> str:
            _git(clone, "checkout", f"origin/{_PR_BRANCH}")
            return initial_slug

        with (
            patch("teatree.core.merge.execution.find_project_root", return_value=clone),
            patch(
                "teatree.core.merge.execution._reconcile_slug_against_reviewed_sha",
                side_effect=_probe_does_local_checkout,
            ),
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_gh_ok),
        ):
            return merge_ticket_pr(clear=_clear(_ticket()), executing_loop_identity="merge-loop")

    def test_clone_stays_on_main_after_probe_checkout(self) -> None:
        clone = _make_clone(self.tmp_path)
        assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main"

        outcome = self._run_with_probe_checkout(clone)

        assert getattr(outcome, "merged_sha", "")
        # The whole point of #2383: NOT detached, NOT left on the PR branch.
        assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main", (
            "keystone merge left the caller's clone off main — the next "
            "`git pull --ff-only origin/main` would abort (Not possible to fast-forward)"
        )
        # And ff-only sync — the operation #2383 said broke — now succeeds.
        ff = subprocess.run(
            ["git", "-C", str(clone), "merge", "--ff-only", "origin/main"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        assert ff.returncode == 0, f"ff-only sync aborted after merge: {ff.stderr}"

    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path


class TestRestoreCallerBranchPrimitive(TestCase):
    """The guard primitive restores a branch / detached SHA across a real checkout."""

    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    def test_restores_branch_after_detaching_checkout(self) -> None:
        clone = _make_clone(self.tmp_path)
        assert _capture_head(str(clone)) == ("main", "")

        with restore_caller_branch(str(clone)):
            _git(clone, "checkout", f"origin/{_PR_BRANCH}")
            assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"  # detached

        assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main"

    def test_restores_branch_even_when_body_raises(self) -> None:
        clone = _make_clone(self.tmp_path)

        def _checkout_then_fail() -> None:
            with restore_caller_branch(str(clone)):
                _git(clone, "checkout", f"origin/{_PR_BRANCH}")
                msg = "merge refused"
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="merge refused"):
            _checkout_then_fail()

        assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main"

    def test_none_repo_is_a_noop(self) -> None:
        with restore_caller_branch(None):
            pass  # no repo to guard; must not raise

    def test_already_on_captured_branch_leaves_head_untouched(self) -> None:
        clone = _make_clone(self.tmp_path)
        before = _git(clone, "rev-parse", "HEAD")
        with restore_caller_branch(str(clone)):
            pass
        assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main"
        assert _git(clone, "rev-parse", "HEAD") == before

    def test_detached_start_restores_to_original_sha_not_a_branch(self) -> None:
        """Caller already on a detached HEAD: restore returns to that exact SHA (#2383).

        Pins the ``_restore_head`` else-branch
        ``(not current_branch and current_sha == detached_sha)``: the captured
        state is a SHA, not a branch name. Inside the guard a different SHA is
        checked out; on exit HEAD must return to the ORIGINAL detached SHA — not
        a branch name, not the pr-branch tip.
        """
        clone = _make_clone(self.tmp_path)
        main_sha = _git(clone, "rev-parse", "HEAD")
        _git(clone, "checkout", main_sha)  # detach at main's tip
        assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
        assert _capture_head(str(clone)) == ("", main_sha)

        pr_sha = _git(clone, "rev-parse", f"origin/{_PR_BRANCH}")
        assert pr_sha != main_sha
        with restore_caller_branch(str(clone)):
            _git(clone, "checkout", f"origin/{_PR_BRANCH}")
            assert _git(clone, "rev-parse", "HEAD") == pr_sha

        assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"  # still detached
        assert _git(clone, "rev-parse", "HEAD") == main_sha  # the original SHA, not the pr-branch

    def test_capture_head_on_non_git_path_returns_empty(self) -> None:
        """A path git cannot answer for captures ``("", "")`` (line 47).

        Both ``symbolic-ref --quiet`` and ``rev-parse HEAD`` exit non-zero on a
        non-git directory, so the guard has nothing to restore and stays a
        no-op (rather than capturing a bogus ref it would later try to check
        out).
        """
        not_a_repo = self.tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        assert _capture_head(str(not_a_repo)) == ("", "")

        # And the whole guard is inert on such a path — it must not raise.
        with restore_caller_branch(str(not_a_repo)):
            pass

    def test_restore_head_with_empty_target_is_a_noop(self) -> None:
        """``_restore_head`` with neither branch nor SHA returns early (line 54)."""
        clone = _make_clone(self.tmp_path)
        before = _git(clone, "rev-parse", "HEAD")
        _restore_head(str(clone), "", "")
        assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main"
        assert _git(clone, "rev-parse", "HEAD") == before

    def test_restore_failure_logs_warning_and_does_not_raise(self) -> None:
        """A checkout that cannot complete logs a warning, never raises (line 62).

        The restore target is overwritten in the worktree with conflicting
        uncommitted changes, so a plain ``git checkout main`` refuses (it would
        clobber the local edit). The guard is best-effort: it logs the recovery
        hint and returns, never propagating the failure.
        """
        clone = _make_clone(self.tmp_path)
        _git(clone, "checkout", f"origin/{_PR_BRANCH}")  # detach off main
        # A conflicting uncommitted edit to a file that differs between the
        # detached tree and main makes `git checkout main` refuse.
        (clone / "f.txt").write_text("conflicting local edit\n", encoding="utf-8")

        with self.assertLogs("teatree.core.merge.head_guard", level="WARNING") as logs:
            _restore_head(str(clone), "main", "")

        assert any("could not restore" in message for message in logs.output)
        assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"  # checkout refused; still detached

    def test_guard_swallows_restore_exception_and_propagates_body_result(self) -> None:
        """The crash-proof branch: a restore that raises never masks the body (lines 89-90).

        ``_restore_head`` is patched to raise inside the guard's ``finally``. The
        guard must swallow it (logging an exception) AND let the body's own
        return value propagate unchanged — the docstring's "best-effort and
        crash-proof" claim, proven.
        """
        clone = _make_clone(self.tmp_path)
        boom = RuntimeError("restore blew up")

        def _explode(*_args: object, **_kwargs: object) -> None:
            raise boom

        with (
            patch("teatree.core.merge.head_guard._restore_head", side_effect=_explode),
            self.assertLogs("teatree.core.merge.head_guard", level="ERROR") as logs,
        ):
            with restore_caller_branch(str(clone)):
                body_result = "body ran to completion"
            # The guard did not re-raise the restore failure: we reach here.
            assert body_result == "body ran to completion"

        assert any("restore raised" in message for message in logs.output)

    def test_guard_swallows_restore_exception_and_propagates_body_exception(self) -> None:
        """A body exception still propagates even when restore also raises (lines 89-90)."""
        clone = _make_clone(self.tmp_path)

        def _explode(*_args: object, **_kwargs: object) -> None:
            msg = "restore blew up"
            raise RuntimeError(msg)

        def _body_raises() -> None:
            with restore_caller_branch(str(clone)):
                msg = "original body failure"
                raise ValueError(msg)

        # The ORIGINAL body exception wins; the restore exception is swallowed.
        with (
            patch("teatree.core.merge.head_guard._restore_head", side_effect=_explode),
            self.assertLogs("teatree.core.merge.head_guard", level="ERROR"),
            pytest.raises(ValueError, match="original body failure"),
        ):
            _body_raises()
