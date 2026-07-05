"""Conflict-only merge-commit detection + clearance re-bind — real git (PR-07)."""

import subprocess
import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import review as review_cmd
from teatree.core.merge import conflict_only as co
from teatree.core.merge.conflict_only import (
    is_conflict_only_merge_commit,
    merge_commit_parents,
    rebind_clearance_after_conflict_only_merge,
)
from teatree.core.models import ClearRequest, MergeClear, ReviewVerdict, ReviewVerdictError, Ticket

_REVIEWER = "cold-reviewer-7"


def _carry_forward_raises(self: ReviewVerdict, *, reviewed_sha: str) -> ReviewVerdict:
    """A ``carry_forward`` stub that models a genuinely-unwaivable source row."""
    msg = "unwaivable"
    raise ReviewVerdictError(msg)


def _fake_git(returncode: int = 0, stdout: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        msg = f"git {' '.join(args)} failed: {result.stderr}"
        raise AssertionError(msg)
    return result.stdout.strip()


def _init_repo(repo: Path, *, conflict_name: str = "conflict.txt") -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / conflict_name).write_text("line1\nline2\nline3\n")
    (repo / "other.txt").write_text("other\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")


def _diverge(repo: Path, feature: str, *, conflict_name: str = "conflict.txt") -> None:
    """Create a conflicting edit on ``main`` and on a fresh ``feature`` branch."""
    _git(repo, "checkout", "-q", "main")
    (repo / conflict_name).write_text("line1\nmain-change\nline3\n")
    _git(repo, "commit", "-q", "-am", "main edit")
    _git(repo, "checkout", "-q", "-b", feature, "main~1")
    (repo / conflict_name).write_text("line1\nfeature-change\nline3\n")
    _git(repo, "commit", "-q", "-am", "feature edit")


def _merge_main_resolving(repo: Path, *, extra_edit: bool, conflict_name: str = "conflict.txt") -> str:
    """Merge main into the current branch, resolve the conflict, return the merge SHA.

    ``extra_edit`` additionally edits a cleanly-merged file → a substantive
    ("evil") merge that is NOT conflict-only.
    """
    subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-edit", "main"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    (repo / conflict_name).write_text("line1\nfeature-change\nmain-change\nline3\n")
    if extra_edit:
        (repo / "other.txt").write_text("evil-change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-edit")
    return _git(repo, "rev-parse", "HEAD")


class TestIsConflictOnlyMergeCommit:
    def test_conflict_only_resolution_is_detected(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _diverge(repo, "feature")
        merge_sha = _merge_main_resolving(repo, extra_edit=False)
        assert is_conflict_only_merge_commit(str(repo), merge_sha) is True

    def test_substantive_evil_merge_is_not_conflict_only(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _diverge(repo, "feature")
        merge_sha = _merge_main_resolving(repo, extra_edit=True)
        assert is_conflict_only_merge_commit(str(repo), merge_sha) is False

    def test_plain_non_merge_commit_is_not_conflict_only(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        head = _git(repo, "rev-parse", "HEAD")
        assert is_conflict_only_merge_commit(str(repo), head) is False

    def test_unknown_sha_is_not_conflict_only(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        assert is_conflict_only_merge_commit(str(repo), "0" * 40) is False

    def test_two_parents_reported_for_a_merge(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _diverge(repo, "feature")
        merge_sha = _merge_main_resolving(repo, extra_edit=False)
        assert len(merge_commit_parents(str(repo), merge_sha)) == 2


class TestConflictOnlyFailsSafe:
    """Every git uncertainty resolves to NOT conflict-only (force re-review)."""

    _P1 = "a" * 40
    _P2 = "b" * 40
    _MERGE = "c" * 40

    def test_unparseable_auto_merge_tree_is_not_conflict_only(self) -> None:
        with (
            patch.object(co, "merge_commit_parents", return_value=(self._P1, self._P2)),
            patch.object(co, "_git", return_value=_fake_git(returncode=1, stdout="")),
        ):
            assert is_conflict_only_merge_commit("/repo", self._MERGE) is False

    def test_unparseable_merge_commit_tree_is_not_conflict_only(self) -> None:
        seq = [_fake_git(stdout="d" * 40), _fake_git(stdout="not-an-oid")]
        with (
            patch.object(co, "merge_commit_parents", return_value=(self._P1, self._P2)),
            patch.object(co, "_git", side_effect=seq),
        ):
            assert is_conflict_only_merge_commit("/repo", self._MERGE) is False

    def test_diff_error_is_not_conflict_only(self) -> None:
        seq = [_fake_git(stdout="d" * 40), _fake_git(stdout="e" * 40), _fake_git(returncode=1)]
        with (
            patch.object(co, "merge_commit_parents", return_value=(self._P1, self._P2)),
            patch.object(co, "_git", side_effect=seq),
        ):
            assert is_conflict_only_merge_commit("/repo", self._MERGE) is False


def _init_repo_marker_file(repo: Path) -> None:
    """Like ``_init_repo`` but ``other.txt`` legitimately CONTAINS conflict markers.

    A doc/fixture whose content includes literal ``<<<<<<<`` / ``>>>>>>>`` lines.
    ``other.txt`` is cleanly merged (untouched on both sides), so its marker
    content flows verbatim into git's auto-merge tree — the exact shape a
    marker-grep oracle misreads as "was conflicted".
    """
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "conflict.txt").write_text("line1\nline2\nline3\n")
    (repo / "other.txt").write_text("<<<<<<< example\nsome doc text\n>>>>>>> example\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")


class TestEvilMergeOnMarkerContainingFile:
    """PR-07 fail-open: an evil merge editing a cleanly-merged marker-containing file.

    ``other.txt`` merges cleanly yet its content carries literal conflict markers.
    The evil merge resolves ``conflict.txt`` AND edits ``other.txt``. A marker-grep
    oracle sees markers in the auto-merge blob of ``other.txt`` and misclassifies
    the substantive deviation as conflict-only — re-binding clearance and SKIPPING
    re-review. git's authoritative conflicted-path set contains only ``conflict.txt``,
    so the deviation on ``other.txt`` correctly forces a fresh review.
    """

    def test_evil_edit_on_marker_file_is_not_conflict_only(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo_marker_file(repo)
        _diverge(repo, "feature")
        merge_sha = _merge_main_resolving(repo, extra_edit=True)
        # RED without the fix: the marker-grep oracle returns True (misclassified
        # conflict-only) and this assertion fails; git's conflicted-path set returns
        # False (re-review forced).
        assert is_conflict_only_merge_commit(str(repo), merge_sha) is False

    def test_marker_file_untouched_conflict_only_still_detected(self, tmp_path: Path) -> None:
        # Control: with NO evil edit, the only deviation IS the genuinely-conflicted
        # file, so the merge stays conflict-only even though other.txt carries markers.
        repo = tmp_path / "repo"
        _init_repo_marker_file(repo)
        _diverge(repo, "feature")
        merge_sha = _merge_main_resolving(repo, extra_edit=False)
        assert is_conflict_only_merge_commit(str(repo), merge_sha) is True


class TestNonAsciiConflictedPathEncoding:
    r"""CORR-07b: a conflicted path with a non-ASCII name classifies verbatim.

    The conflicted-path set is read ``-z`` (verbatim ``café.py``); before the fix
    the deviation diff was read WITHOUT ``-z``, so git C-quoted the same path to
    ``"caf\303\251.py"`` under ``core.quotePath``. The quoted deviation name never
    matched the verbatim conflicted-path name, so a genuine conflict-only merge
    over-blocked (a legit merge forced back to re-review) and a decoy could fail
    OPEN. Both sides now read ``-z``.
    """

    _NON_ASCII = "café.py"

    def test_conflict_only_on_non_ascii_path_is_detected(self, tmp_path: Path) -> None:
        # RED without the -z fix: the C-quoted deviation name misses the verbatim
        # conflicted-path set, so this legit conflict-only merge returns False
        # (over-block).
        repo = tmp_path / "repo"
        _init_repo(repo, conflict_name=self._NON_ASCII)
        _diverge(repo, "feature", conflict_name=self._NON_ASCII)
        merge_sha = _merge_main_resolving(repo, extra_edit=False, conflict_name=self._NON_ASCII)
        assert is_conflict_only_merge_commit(str(repo), merge_sha) is True

    def test_evil_edit_alongside_non_ascii_conflict_is_not_conflict_only(self, tmp_path: Path) -> None:
        # A substantive edit on a cleanly-merged file alongside the non-ASCII
        # conflict resolution is NOT conflict-only — other.txt is absent from the
        # conflicted set, forcing a fresh review.
        repo = tmp_path / "repo"
        _init_repo(repo, conflict_name=self._NON_ASCII)
        _diverge(repo, "feature", conflict_name=self._NON_ASCII)
        merge_sha = _merge_main_resolving(repo, extra_edit=True, conflict_name=self._NON_ASCII)
        assert is_conflict_only_merge_commit(str(repo), merge_sha) is False


def _clear_with_verdict(repo: Path, feature_tip: str) -> MergeClear:
    ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
    clear = MergeClear.issue(
        ClearRequest(
            pr_id=42,
            slug="souliane/teatree",
            reviewed_sha=feature_tip,
            reviewer_identity=_REVIEWER,
            ticket=ticket,
        ),
    )
    ReviewVerdict.record(
        pr_id=42,
        slug="souliane/teatree",
        reviewed_sha=feature_tip,
        verdict=ReviewVerdict.Verdict.MERGE_SAFE,
        reviewer_identity=_REVIEWER,
        ticket=ticket,
    )
    return clear


def _expedited_pending_clear_with_verdict(feature_tip: str) -> MergeClear:
    """An expedited (PENDING-checks) CLEAR + its sibling PENDING merge_safe verdict.

    Models the human-authorized expedite waiver (§17.4.3): the ticket is flagged
    expedited, the CLEAR carries a recorded authoriser and a full-CI-green
    attestation bound to the reviewed tree, and the verdict snapshots ``pending``
    checks under ``expedited=True``.
    """
    ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW, expedited=True)
    clear = MergeClear.issue(
        ClearRequest(
            pr_id=42,
            slug="souliane/teatree",
            reviewed_sha=feature_tip,
            reviewer_identity=_REVIEWER,
            gh_verify_result=MergeClear.VerifyResult.PENDING,
            blast_class=MergeClear.BlastClass.DOCS,
            ticket=ticket,
            expedite_authorizer="owner-x",
            local_ci_green_sha=feature_tip,
        ),
    )
    ReviewVerdict.record(
        pr_id=42,
        slug="souliane/teatree",
        reviewed_sha=feature_tip,
        verdict=ReviewVerdict.Verdict.MERGE_SAFE,
        reviewer_identity=_REVIEWER,
        gh_verify_result=MergeClear.VerifyResult.PENDING,
        blast_class=MergeClear.BlastClass.DOCS,
        ticket=ticket,
        expedited=True,
    )
    return clear


class TestRebindClearance(TestCase):
    def test_conflict_only_merge_rebinds_clearance_and_verdict(self) -> None:
        repo = Path(self._repo())
        _diverge(repo, "feature")
        feature_tip = _git(repo, "rev-parse", "feature")
        clear = _clear_with_verdict(repo, feature_tip)
        merge_sha = _merge_main_resolving(repo, extra_edit=False)

        rebound = rebind_clearance_after_conflict_only_merge(clear=clear, merge_sha=merge_sha, repo_root=str(repo))

        assert rebound is True
        clear.refresh_from_db()
        assert clear.reviewed_sha == merge_sha.lower()
        assert ReviewVerdict.objects.filter(
            pr_id=42,
            reviewed_sha=merge_sha.lower(),
            verdict=ReviewVerdict.Verdict.MERGE_SAFE,
        ).exists()

    def test_expedited_pending_clear_rebinds_preserving_the_waiver(self) -> None:
        # RED before the fix: the carry-forward re-records the PENDING merge_safe
        # verdict WITHOUT the expedite waiver, so ReviewVerdict.record raises an
        # unhandled ReviewVerdictError and the whole rebind crashes.
        repo = Path(self._repo())
        _diverge(repo, "feature")
        feature_tip = _git(repo, "rev-parse", "feature")
        clear = _expedited_pending_clear_with_verdict(feature_tip)
        merge_sha = _merge_main_resolving(repo, extra_edit=False)

        rebound = rebind_clearance_after_conflict_only_merge(clear=clear, merge_sha=merge_sha, repo_root=str(repo))

        assert rebound is True
        clear.refresh_from_db()
        assert clear.reviewed_sha == merge_sha.lower()
        carried = ReviewVerdict.objects.get(
            pr_id=42,
            reviewed_sha=merge_sha.lower(),
            verdict=ReviewVerdict.Verdict.MERGE_SAFE,
        )
        # The waiver is preserved: the carried-forward verdict keeps the PENDING
        # snapshot (it could only exist under the human-authorized waiver).
        assert carried.gh_verify_result == MergeClear.VerifyResult.PENDING
        assert carried.reviewer_identity == _REVIEWER

    def test_rebind_refuses_cleanly_when_a_verdict_cannot_be_carried_forward(self) -> None:
        # A genuinely-unwaivable carry-forward must refuse cleanly (return False,
        # atomic rollback), never surface a ReviewVerdictError traceback.
        repo = Path(self._repo())
        _diverge(repo, "feature")
        feature_tip = _git(repo, "rev-parse", "feature")
        clear = _clear_with_verdict(repo, feature_tip)
        merge_sha = _merge_main_resolving(repo, extra_edit=False)

        with patch.object(ReviewVerdict, "carry_forward", _carry_forward_raises):
            rebound = rebind_clearance_after_conflict_only_merge(clear=clear, merge_sha=merge_sha, repo_root=str(repo))

        assert rebound is False
        clear.refresh_from_db()
        assert clear.reviewed_sha == feature_tip.lower()
        assert not ReviewVerdict.objects.filter(reviewed_sha=merge_sha.lower()).exists()

    def test_rebind_command_refuses_cleanly_when_carry_forward_fails(self) -> None:
        # The CLI boundary renders the clean refusal (no traceback) too.
        repo = Path(self._repo())
        _diverge(repo, "feature")
        feature_tip = _git(repo, "rev-parse", "feature")
        clear = _clear_with_verdict(repo, feature_tip)
        merge_sha = _merge_main_resolving(repo, extra_edit=False)

        with patch.object(ReviewVerdict, "carry_forward", _carry_forward_raises):
            result = cast(
                "dict[str, object]",
                call_command("review", "rebind-clearance", str(clear.pk), merge_sha=merge_sha, repo_root=str(repo)),
            )
        assert result["rebound"] is False
        assert result["reviewed_sha"] == feature_tip.lower()

    def test_substantive_merge_does_not_rebind(self) -> None:
        repo = Path(self._repo())
        _diverge(repo, "feature")
        feature_tip = _git(repo, "rev-parse", "feature")
        clear = _clear_with_verdict(repo, feature_tip)
        merge_sha = _merge_main_resolving(repo, extra_edit=True)

        rebound = rebind_clearance_after_conflict_only_merge(clear=clear, merge_sha=merge_sha, repo_root=str(repo))

        assert rebound is False
        clear.refresh_from_db()
        assert clear.reviewed_sha == feature_tip.lower()
        assert not ReviewVerdict.objects.filter(reviewed_sha=merge_sha.lower()).exists()

    def test_first_parent_mismatch_does_not_rebind(self) -> None:
        repo = Path(self._repo())
        _diverge(repo, "feature")
        feature_tip = _git(repo, "rev-parse", "feature")
        merge_sha = _merge_main_resolving(repo, extra_edit=False)
        # A CLEAR reviewed at a SHA that is NOT the merge's first parent.
        clear = _clear_with_verdict(repo, "f" * 40)

        rebound = rebind_clearance_after_conflict_only_merge(clear=clear, merge_sha=merge_sha, repo_root=str(repo))
        assert rebound is False
        assert feature_tip  # feature tip exists but is not the CLEAR's reviewed_sha

    def test_non_hex_merge_sha_does_not_rebind(self) -> None:
        repo = Path(self._repo())
        _diverge(repo, "feature")
        feature_tip = _git(repo, "rev-parse", "feature")
        clear = _clear_with_verdict(repo, feature_tip)
        assert rebind_clearance_after_conflict_only_merge(clear=clear, merge_sha="z" * 40, repo_root=str(repo)) is False

    def test_no_merge_safe_verdict_does_not_rebind(self) -> None:
        repo = Path(self._repo())
        _diverge(repo, "feature")
        feature_tip = _git(repo, "rev-parse", "feature")
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        # A CLEAR with NO recorded verdict at the reviewed tree.
        clear = MergeClear.issue(
            ClearRequest(
                pr_id=42, slug="souliane/teatree", reviewed_sha=feature_tip, reviewer_identity=_REVIEWER, ticket=ticket
            ),
        )
        merge_sha = _merge_main_resolving(repo, extra_edit=False)
        assert (
            rebind_clearance_after_conflict_only_merge(clear=clear, merge_sha=merge_sha, repo_root=str(repo)) is False
        )

    def test_later_hold_at_reviewed_tree_refuses_rebind(self) -> None:
        repo = Path(self._repo())
        _diverge(repo, "feature")
        feature_tip = _git(repo, "rev-parse", "feature")
        clear = _clear_with_verdict(repo, feature_tip)
        # A later independent HOLD at the same reviewed tree supersedes the merge_safe.
        ReviewVerdict.record(
            pr_id=42,
            slug="souliane/teatree",
            reviewed_sha=feature_tip,
            verdict=ReviewVerdict.Verdict.HOLD,
            reviewer_identity=_REVIEWER,
        )
        merge_sha = _merge_main_resolving(repo, extra_edit=False)
        assert (
            rebind_clearance_after_conflict_only_merge(clear=clear, merge_sha=merge_sha, repo_root=str(repo)) is False
        )

    def test_rebind_command_rebinds_a_conflict_only_merge(self) -> None:
        repo = Path(self._repo())
        _diverge(repo, "feature")
        feature_tip = _git(repo, "rev-parse", "feature")
        clear = _clear_with_verdict(repo, feature_tip)
        merge_sha = _merge_main_resolving(repo, extra_edit=False)
        result = cast(
            "dict[str, object]",
            call_command("review", "rebind-clearance", str(clear.pk), merge_sha=merge_sha, repo_root=str(repo)),
        )
        assert result["rebound"] is True
        assert result["reviewed_sha"] == merge_sha.lower()

    def test_rebind_command_refuses_a_substantive_merge(self) -> None:
        repo = Path(self._repo())
        _diverge(repo, "feature")
        feature_tip = _git(repo, "rev-parse", "feature")
        clear = _clear_with_verdict(repo, feature_tip)
        merge_sha = _merge_main_resolving(repo, extra_edit=True)
        result = cast(
            "dict[str, object]",
            call_command("review", "rebind-clearance", str(clear.pk), merge_sha=merge_sha, repo_root=str(repo)),
        )
        assert result["rebound"] is False
        assert result["reviewed_sha"] == feature_tip.lower()

    def test_rebind_command_missing_clear_exits(self) -> None:
        with pytest.raises(SystemExit):
            call_command("review", "rebind-clearance", "999999", merge_sha="a" * 40)

    def test_rebind_command_missing_merge_sha_exits(self) -> None:
        with pytest.raises(SystemExit):
            call_command("review", "rebind-clearance", "1")

    def _repo(self) -> str:
        path = Path(tempfile.mkdtemp())
        _init_repo(path)
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(path)], check=False))  # noqa: S607
        return str(path)


class TestProjectRootFallback:
    def test_returns_project_root_when_resolved(self) -> None:
        with patch.object(review_cmd, "find_project_root", return_value=Path("/repo/root")):
            assert review_cmd._project_root_or_cwd() == "/repo/root"

    def test_falls_back_to_dot_when_unresolved(self) -> None:
        with patch.object(review_cmd, "find_project_root", return_value=None):
            assert review_cmd._project_root_or_cwd() == "."
