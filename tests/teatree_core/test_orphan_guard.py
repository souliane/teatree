from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.forge_pr_probe import PrProbe
from teatree.core.gates.orphan_guard import BranchReport, BranchStatus, classify_branch, find_orphans_in_workspace
from teatree.core.models import Ticket, Worktree
from teatree.core.worktree.branch_classification import BranchCommit, SubjectPrefilterResult
from teatree.utils.run import CommandFailedError
from tests._git_repo import make_git_repo, run_git
from tests.teatree_core.cleanup._shared import _run_git

_patch_classify = patch("teatree.core.gates.orphan_guard.prefilter_branch_commits_by_subject")
_patch_tree_match = patch("teatree.core.gates.orphan_guard._branch_tree_matches_squash")
_patch_open_pr = patch("teatree.core.gates.orphan_guard.find_open_pr_for_branch")
_patch_git = patch("teatree.core.gates.orphan_guard.git")


def _classification(ahead: list[BranchCommit] | None = None) -> SubjectPrefilterResult:
    return SubjectPrefilterResult(genuinely_ahead=ahead or [])


def _commit(sha: str = "abc", subject: str = "feat: x") -> BranchCommit:
    return BranchCommit(sha=sha, subject=subject, is_merge=False)


class TestClassifyBranch(TestCase):
    @_patch_classify
    def test_synced_when_no_commits_ahead(self, mock_classify: MagicMock) -> None:
        mock_classify.return_value = _classification()
        report = classify_branch("/repo", "feature")
        assert report.status is BranchStatus.SYNCED
        assert report.ahead_count == 0
        assert not report.is_orphan

    @_patch_tree_match
    @_patch_classify
    def test_synced_when_tree_matches_squash_commit(
        self,
        mock_classify: MagicMock,
        mock_tree_match: MagicMock,
    ) -> None:
        mock_classify.return_value = _classification([_commit()])
        mock_tree_match.return_value = True
        report = classify_branch("/repo", "feature")
        assert report.status is BranchStatus.SYNCED

    @_patch_open_pr
    @_patch_tree_match
    @_patch_classify
    def test_open_pr_when_branch_has_open_pr(
        self,
        mock_classify: MagicMock,
        mock_tree_match: MagicMock,
        mock_open_pr: MagicMock,
    ) -> None:
        mock_classify.return_value = _classification([_commit()])
        mock_tree_match.return_value = False
        mock_open_pr.return_value = PrProbe.found("https://github.com/org/repo/pull/42")
        report = classify_branch("/repo", "feature")
        assert report.status is BranchStatus.OPEN_PR
        assert report.open_pr_url == "https://github.com/org/repo/pull/42"
        assert not report.is_orphan

    @_patch_git
    @_patch_open_pr
    @_patch_tree_match
    @_patch_classify
    def test_pushed_orphan_when_remote_exists_but_no_open_pr(
        self,
        mock_classify: MagicMock,
        mock_tree_match: MagicMock,
        mock_open_pr: MagicMock,
        mock_git: MagicMock,
    ) -> None:
        mock_classify.return_value = _classification([_commit(), _commit("def", "feat: y")])
        mock_tree_match.return_value = False
        mock_open_pr.return_value = PrProbe.none()
        mock_git.run.return_value = "abc123\trefs/heads/feature"
        report = classify_branch("/repo", "feature")
        assert report.status is BranchStatus.PUSHED_ORPHAN
        assert report.ahead_count == 2
        assert report.is_orphan

    @_patch_git
    @_patch_open_pr
    @_patch_tree_match
    @_patch_classify
    def test_unpushed_orphan_when_no_remote_branch_exists(
        self,
        mock_classify: MagicMock,
        mock_tree_match: MagicMock,
        mock_open_pr: MagicMock,
        mock_git: MagicMock,
    ) -> None:
        mock_classify.return_value = _classification([_commit()])
        mock_tree_match.return_value = False
        mock_open_pr.return_value = PrProbe.none()
        mock_git.run.return_value = ""
        report = classify_branch("/repo", "feature")
        assert report.status is BranchStatus.UNPUSHED_ORPHAN
        assert report.is_orphan


class TestClassifyBranchWhenTheProbeCannotAnswer(TestCase):
    """#4116: a can't-tell probe is its own state, never a confident "no PR".

    Collapsing UNKNOWN into "no open PR" made an unreadable forge look exactly
    like a branch that owes a PR, so the pre-push gate tried to open one for a
    branch that already had it and refused the push on the resulting
    ``already exists``.
    """

    @_patch_git
    @_patch_open_pr
    @_patch_tree_match
    @_patch_classify
    def test_unknown_probe_is_not_reported_as_an_orphan(
        self,
        mock_classify: MagicMock,
        mock_tree_match: MagicMock,
        mock_open_pr: MagicMock,
        mock_git: MagicMock,
    ) -> None:
        mock_classify.return_value = _classification([_commit()])
        mock_tree_match.return_value = False
        mock_open_pr.return_value = PrProbe.unknown()
        mock_git.run.return_value = "abc123\trefs/heads/feature"

        report = classify_branch("/repo", "feature")

        assert report.status is BranchStatus.PR_UNKNOWN
        assert not report.is_orphan


class TestClassifyBranchWhoseWorkAlreadyLanded:
    """#3977: work that reached the base under a DIFFERENT path owes no PR.

    Real git — the defect is a path-level comparison missing content the base
    carries elsewhere, so git's own blob identity has to be in the loop.
    """

    FIX = "def parse(raw: str) -> int:\n    return int(raw.strip() or 0)\n"

    @pytest.fixture(autouse=True)
    def _repo_with_the_fix_on_a_branch(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin.git"
        _run_git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
        self.clone = tmp_path / "clone"
        _run_git("clone", "-q", str(origin), str(self.clone), cwd=tmp_path)
        _run_git("config", "user.email", "t@t", cwd=self.clone)
        _run_git("config", "user.name", "t", cwd=self.clone)
        (self.clone / "app").mkdir()
        (self.clone / "app" / "parse.py").write_text("def parse(raw):\n    return 0\n")
        _run_git("add", "-A", cwd=self.clone)
        _run_git("commit", "-q", "-m", "initial module", cwd=self.clone)
        _run_git("push", "-q", "origin", "main", cwd=self.clone)

        _run_git("checkout", "-q", "-b", "fix/parse", cwd=self.clone)
        (self.clone / "app" / "parse.py").write_text(self.FIX)
        _run_git("add", "-A", cwd=self.clone)
        _run_git("commit", "-q", "-m", "fix(parse): honour whitespace", cwd=self.clone)
        _run_git("checkout", "-q", "main", cwd=self.clone)

    def _land_the_fix_under_a_different_path(self) -> None:
        (self.clone / "core").mkdir()
        (self.clone / "core" / "parsing.py").write_text(self.FIX)
        (self.clone / "app" / "parse.py").unlink()
        _run_git("add", "-A", cwd=self.clone)
        _run_git("commit", "-q", "-m", "refactor(core): split the parsing module", cwd=self.clone)
        _run_git("push", "-q", "origin", "main", cwd=self.clone)

    @_patch_open_pr
    @_patch_tree_match
    def test_synced_once_the_same_bytes_are_on_the_base_elsewhere(
        self,
        mock_tree_match: MagicMock,
        mock_open_pr: MagicMock,
    ) -> None:
        mock_tree_match.return_value = False
        mock_open_pr.return_value = PrProbe.none()
        self._land_the_fix_under_a_different_path()

        report = classify_branch(str(self.clone), "fix/parse")

        assert report.status is BranchStatus.SYNCED
        assert not report.is_orphan

    @_patch_open_pr
    @_patch_tree_match
    def test_still_an_orphan_while_the_base_genuinely_lacks_the_content(
        self,
        mock_tree_match: MagicMock,
        mock_open_pr: MagicMock,
    ) -> None:
        mock_tree_match.return_value = False
        mock_open_pr.return_value = PrProbe.none()

        report = classify_branch(str(self.clone), "fix/parse")

        assert report.is_orphan


_SQUASHED_BRANCH = "feat/parse"


def _commit_all(repo: Path, message: str) -> None:
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message)


def _clone_whose_branch_squash_merged_then_merged_the_base_back(tmp_path: Path) -> Path:
    """A branch whose work squash-merged, that then merged the moved-on base back in.

    The #4429 shape: the squash rewrote the branch's commits into a new SHA on the
    base, so every graph-level probe still reads the branch as ahead, and merging
    the base back left its tree identical to a base commit — a pull request from it
    would carry nothing at all. The base then moves once more, so the merge base is
    an ancestor rather than the base tip and only a three-dot read sees the emptiness.
    """
    origin = make_git_repo(tmp_path / "origin.git", bare=True)
    clone = tmp_path / "clone"
    run_git(tmp_path, "clone", "-q", str(origin), str(clone))
    (clone / "parse.py").write_text("def parse(raw):\n    return 0\n")
    _commit_all(clone, "initial module")
    run_git(clone, "push", "-q", "origin", "main")

    run_git(clone, "checkout", "-q", "-b", _SQUASHED_BRANCH)
    (clone / "parse.py").write_text("def parse(raw: str) -> int:\n    return int(raw.strip() or 0)\n")
    _commit_all(clone, "fix(parse): honour whitespace")
    (clone / "test_parse.py").write_text("def test_parse() -> None:\n    assert parse(' 7 ') == 7\n")
    _commit_all(clone, "test(parse): cover the whitespace case")
    run_git(clone, "push", "-q", "origin", _SQUASHED_BRANCH)

    run_git(clone, "checkout", "-q", "main")
    run_git(clone, "merge", "-q", "--squash", _SQUASHED_BRANCH)
    _commit_all(clone, "fix(parse): honour whitespace (#4422)")
    run_git(clone, "push", "-q", "origin", "main")

    run_git(clone, "checkout", "-q", _SQUASHED_BRANCH)
    run_git(clone, "merge", "-q", "--no-edit", "origin/main")
    run_git(clone, "push", "-q", "origin", _SQUASHED_BRANCH)

    run_git(clone, "checkout", "-q", "main")
    (clone / "CHANGELOG.md").write_text("the base moves on\n")
    _commit_all(clone, "docs: unrelated base work")
    run_git(clone, "push", "-q", "origin", "main")
    return clone


class TestClassifyBranchWhoseContentAlreadySquashMerged:
    """#4429: a branch that can only open an empty pull request never owes one.

    Real git — the defect is that the squash rewrites SHAs, so the graph says
    "ahead" while the diff the forge would compute is empty. Twice in one evening a
    cold review was spent on a 0-file PR opened from such a branch.
    """

    @pytest.fixture(autouse=True)
    def _clone_with_the_squashed_branch(self, tmp_path: Path) -> None:
        self.clone = _clone_whose_branch_squash_merged_then_merged_the_base_back(tmp_path)

    @_patch_open_pr
    @_patch_tree_match
    def test_a_branch_that_would_deliver_nothing_owes_no_pull_request(
        self,
        mock_tree_match: MagicMock,
        mock_open_pr: MagicMock,
    ) -> None:
        mock_tree_match.return_value = False
        mock_open_pr.return_value = PrProbe.none()

        report = classify_branch(str(self.clone), _SQUASHED_BRANCH)

        assert not report.is_orphan
        assert report.status is BranchStatus.EMPTY_DELTA

    @_patch_open_pr
    @_patch_tree_match
    def test_work_added_after_the_merge_back_still_owes_one(
        self,
        mock_tree_match: MagicMock,
        mock_open_pr: MagicMock,
    ) -> None:
        mock_tree_match.return_value = False
        mock_open_pr.return_value = PrProbe.none()
        run_git(self.clone, "checkout", "-q", _SQUASHED_BRANCH)
        (self.clone / "parse.py").write_text("def parse(raw: str) -> int:\n    return int(raw.strip() or -1)\n")
        _commit_all(self.clone, "fix(parse): use a sentinel for the empty case")

        report = classify_branch(str(self.clone), _SQUASHED_BRANCH)

        assert report.status is BranchStatus.PUSHED_ORPHAN
        assert report.is_orphan


class TestClassifyBranchCarryingNoContentOfItsOwn:
    """A branch ahead by SHA alone can only open a zero-file pull request either."""

    @_patch_open_pr
    @_patch_tree_match
    def test_a_content_free_commit_owes_no_pull_request(
        self,
        mock_tree_match: MagicMock,
        mock_open_pr: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_tree_match.return_value = False
        mock_open_pr.return_value = PrProbe.none()
        origin = make_git_repo(tmp_path / "origin.git", bare=True)
        clone = tmp_path / "clone"
        run_git(tmp_path, "clone", "-q", str(origin), str(clone))
        (clone / "app.py").write_text("VALUE = 1\n")
        _commit_all(clone, "initial module")
        run_git(clone, "push", "-q", "origin", "main")
        run_git(clone, "checkout", "-q", "-b", "chore/marker")
        run_git(clone, "commit", "-q", "--allow-empty", "-m", "chore: mark the release")

        report = classify_branch(str(clone), "chore/marker")

        assert report.status is BranchStatus.EMPTY_DELTA
        assert not report.is_orphan


class TestFindOrphansInWorkspace(TestCase):
    def _make_worktree(self, repo_path: str, branch: str) -> Worktree:
        ticket = Ticket.objects.create(
            issue_url=f"https://gitlab.com/org/{repo_path}/-/issues/{branch}",
            state=Ticket.State.IN_REVIEW,
        )
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path=repo_path,
            branch=branch,
        )

    @patch("teatree.core.gates.orphan_guard.clone_root")
    @patch("teatree.core.gates.orphan_guard.classify_branch")
    def test_returns_only_orphans(
        self,
        mock_classify: MagicMock,
        mock_clone_root: MagicMock,
    ) -> None:
        fake_workspace = MagicMock()

        def _fake_div(_self: object, x: str) -> MagicMock:
            return MagicMock(spec=Path, is_dir=lambda: True, __str__=lambda _s: f"/ws/{x}")

        fake_workspace.__truediv__ = _fake_div
        mock_clone_root.return_value = fake_workspace

        self._make_worktree("org/alpha", "feat-1")
        self._make_worktree("org/beta", "feat-2")
        self._make_worktree("org/gamma", "feat-3")

        def classify(_repo: str, branch: str) -> BranchReport:
            statuses = {
                "feat-1": BranchStatus.SYNCED,
                "feat-2": BranchStatus.PUSHED_ORPHAN,
                "feat-3": BranchStatus.UNPUSHED_ORPHAN,
            }
            return BranchReport(repo=_repo, branch=branch, status=statuses[branch], ahead_count=1)

        mock_classify.side_effect = classify

        orphans = find_orphans_in_workspace()

        branches = [o.branch for o in orphans]
        assert "feat-1" not in branches
        assert "feat-2" in branches
        assert "feat-3" in branches
        assert len(orphans) == 2

    @patch("teatree.core.gates.orphan_guard.clone_root")
    @patch("teatree.core.gates.orphan_guard.classify_branch")
    def test_deduplicates_by_repo_and_branch(
        self,
        mock_classify: MagicMock,
        mock_clone_root: MagicMock,
    ) -> None:
        fake_workspace = MagicMock()

        def _fake_div(_self: object, x: str) -> MagicMock:
            return MagicMock(spec=Path, is_dir=lambda: True, __str__=lambda _s: f"/ws/{x}")

        fake_workspace.__truediv__ = _fake_div
        mock_clone_root.return_value = fake_workspace

        self._make_worktree("org/alpha", "feat-1")
        # Same repo+branch across tickets
        ticket2 = Ticket.objects.create(
            issue_url="https://gitlab.com/org/alpha/-/issues/200",
            state=Ticket.State.IN_REVIEW,
        )
        Worktree.objects.create(
            overlay="test",
            ticket=ticket2,
            repo_path="org/alpha",
            branch="feat-1",
        )

        mock_classify.return_value = BranchReport(
            repo="/ws/org/alpha",
            branch="feat-1",
            status=BranchStatus.PUSHED_ORPHAN,
            ahead_count=1,
        )

        orphans = find_orphans_in_workspace()

        assert len(orphans) == 1
        assert mock_classify.call_count == 1

    @patch("teatree.core.gates.orphan_guard.clone_root")
    @patch("teatree.core.gates.orphan_guard.classify_branch")
    def test_skips_worktree_whose_classification_fails_but_reports_the_rest(
        self,
        mock_classify: MagicMock,
        mock_clone_root: MagicMock,
    ) -> None:
        """#2937: one worktree's git failure must not crash the whole scan."""
        fake_workspace = MagicMock()

        def _fake_div(_self: object, x: str) -> MagicMock:
            return MagicMock(spec=Path, is_dir=lambda: True, __str__=lambda _s: f"/ws/{x}")

        fake_workspace.__truediv__ = _fake_div
        mock_clone_root.return_value = fake_workspace

        self._make_worktree("org/alpha", "feat-1")
        self._make_worktree("org/beta", "feat-2")

        def classify(repo: str, branch: str) -> BranchReport:
            if branch == "feat-1":
                raise CommandFailedError(
                    cmd=["git", "-C", repo, "log", branch, "--not", "origin/main"],
                    returncode=128,
                    stdout="",
                    stderr="fatal: cannot change to '/ws/org/alpha': No such file or directory",
                )
            return BranchReport(repo=repo, branch=branch, status=BranchStatus.PUSHED_ORPHAN, ahead_count=1)

        mock_classify.side_effect = classify

        orphans = find_orphans_in_workspace()

        branches = [o.branch for o in orphans]
        assert "feat-1" not in branches
        assert "feat-2" in branches
        assert len(orphans) == 1


class TestClassifyBranchRespectsRepoDefaultBranch:
    """Real-git integration: ``classify_branch`` must use the repo's actual default branch.

    ``prefilter_branch_commits_by_subject`` defaults to ``target="origin/main"``; on a repo
    whose default branch is ``master`` (or anything else), one-commit-ahead-of-
    master was misclassified as SYNCED because the comparison was against the
    non-existent ``origin/main``.
    """

    @pytest.fixture(autouse=True)
    def _tmp_repo_with_master_default(self, tmp_path: Path) -> None:
        self.origin = tmp_path / "origin.git"
        _run_git("init", "-q", "--bare", "-b", "master", str(self.origin), cwd=tmp_path)

        self.clone = tmp_path / "clone"
        _run_git("clone", "-q", str(self.origin), str(self.clone), cwd=tmp_path)
        _run_git("config", "user.email", "t@t", cwd=self.clone)
        _run_git("config", "user.name", "t", cwd=self.clone)
        _run_git("commit", "--allow-empty", "-q", "-m", "initial on master", cwd=self.clone)
        _run_git("push", "-q", "origin", "master", cwd=self.clone)
        _run_git("checkout", "-q", "-b", "feature-branch", cwd=self.clone)
        # Content, not an empty commit: ahead by SHA alone is EMPTY_DELTA (#4429),
        # which would test the wrong branch state.
        (self.clone / "feature.py").write_text("VALUE = 1\n")
        _run_git("add", "-A", cwd=self.clone)
        _run_git("commit", "-q", "-m", "feat: new thing on feature", cwd=self.clone)

    def test_one_commit_ahead_of_master_is_not_classified_as_synced(self) -> None:
        report = classify_branch(str(self.clone), "feature-branch")
        assert report.status is not BranchStatus.SYNCED, (
            "Branch with one unpushed commit on top of origin/master must not "
            "be classified as SYNCED (origin/main was hardcoded)"
        )
        assert report.ahead_count == 1
        assert report.is_orphan

    def test_falls_back_to_origin_main_when_default_branch_undetectable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fallback path — ``git.default_branch`` raises, classifier falls back to ``origin/main``.

        When the repo has no ``origin/HEAD`` and no known fallback name on the
        remote, ``classify_branch`` still attempts ``origin/main``. On this
        fixture that ref genuinely does not exist either (the remote's only
        branch is ``master``), so the underlying ``git log ... --not
        origin/main`` fails for real — and per #2937 that failure must
        propagate (fail loud), never silently degrade to a possibly-wrong
        report.
        """
        from teatree.core.gates import orphan_guard as og  # noqa: PLC0415

        msg = "could not detect default branch"

        def _raise(repo: str) -> str:
            raise RuntimeError(msg)

        monkeypatch.setattr(og.git, "default_branch", _raise)
        with pytest.raises(CommandFailedError, match="origin/main"):
            classify_branch(str(self.clone), "feature-branch")


class TestClassifyBranchFailsLoudOnGitFailure:
    """#2937.

    An invalid ``repo`` filesystem path must fail loud, never silently
    misclassify a genuinely-ahead branch as SYNCED.

    ``t3 <overlay> pr ensure-pr --repo <owner/repo-slug>`` passes a forge
    slug (``owner/repo``) where a filesystem path is expected. ``git -C
    <bad-path> log ...`` then fails, and the classifier used to swallow that
    failure as an empty (legitimately-synced-looking) result.
    """

    def test_nonexistent_repo_path_raises_instead_of_reporting_synced(self, tmp_path: Path) -> None:
        # Never created — mimics passing a forge slug like "owner/repo"
        # instead of a real checkout path.
        bad_repo = str(tmp_path / "owner" / "repo")
        with pytest.raises(CommandFailedError):
            classify_branch(bad_repo, "feature-branch")
