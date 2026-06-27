"""Content-equivalence authorization for the destructive clean-all paths (#2609).

``classify_branch_commits`` buckets a branch's commits ``squash_merged`` purely
by canonicalized-SUBJECT membership in the last N upstream subjects, with NO
content/patch-id check. Subject-matching is fine to *recognize* a forge-squash-
merged candidate (a squash creates a new SHA), but it is unsafe to *authorize*
a destroy: a genuine un-upstreamed commit whose subject collides with an
already-upstreamed subject (e.g. a routine ``docs: update skills``) is
misclassified ``squash_merged`` and would be force-deleted.

#2607 guarded its reset path (``cli/_update_reconcile``) with a ``git cherry``
content gate. These tests pin the SAME content gate on the clean-all force-DELETE
path (``core.cleanup._raise_if_genuinely_ahead``) and assert the shared helper
fails CLOSED on any git error — destruction requires positive proof of content
equivalence.

Real ``git`` under ``tmp_path`` (Test-Writing Doctrine): a true subject
collision cannot be modeled with a mock that returns canned classifications,
because the whole point is that the subject matcher LIES about the content.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.branch_classification import branch_content_upstream, content_equivalence_blockers
from teatree.core.cleanup import CleanupResult, cleanup_worktree
from teatree.core.models import Ticket, Worktree
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git


def _clone_repo(src: Path, dest: Path, *bare: str) -> None:
    subprocess.run(
        [_GIT, "clone", *bare, "-q", str(src), str(dest)],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )


def _git_out(*args: str, cwd: Path) -> str:
    return subprocess.run(
        [_GIT, "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    ).stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run_git("init", "-q", "-b", "main", cwd=path)
    _run_git("config", "user.email", "t@t", cwd=path)
    _run_git("config", "user.name", "t", cwd=path)


class TestContentEquivalenceHelper(TestCase):
    """The shared content-equivalence helper — patch-id authorization, fail-closed."""

    @pytest.fixture(autouse=True)
    def _repo(self, tmp_path: Path) -> None:
        self.bare = tmp_path / "remote.git"
        seed = tmp_path / "seed"
        _init_repo(seed)
        (seed / "f.txt").write_text("v1\n")
        _run_git("add", "f.txt", cwd=seed)
        _run_git("commit", "-q", "-m", "initial", cwd=seed)
        _clone_repo(seed, self.bare, "--bare")
        self.clone = tmp_path / "clone"
        _clone_repo(self.bare, self.clone)
        _run_git("config", "user.email", "t@t", cwd=self.clone)
        _run_git("config", "user.name", "t", cwd=self.clone)

    def _advance_upstream(self, *, subject: str, filename: str, content: str) -> None:
        """Land *content* on the bare remote's main under *subject* (a NEW sha)."""
        work = self.clone.parent / f"upstream-{filename}"
        _clone_repo(self.bare, work)
        _run_git("config", "user.email", "t@t", cwd=work)
        _run_git("config", "user.name", "t", cwd=work)
        (work / filename).write_text(content)
        _run_git("add", filename, cwd=work)
        _run_git("commit", "-q", "-m", subject, cwd=work)
        _run_git("push", "-q", "origin", "main", cwd=work)
        _run_git("fetch", "-q", "origin", cwd=self.clone)

    def test_subject_collision_genuine_commit_is_a_blocker(self) -> None:
        """A genuine commit whose subject collides with an upstream subject is NOT proven upstream.

        The local commit and the upstream commit share the subject
        ``docs: update skills`` but carry DIFFERENT content, so ``git cherry``
        reports the local patch as ``+`` (not upstream). The helper must list it
        as a blocker — content equivalence does NOT hold.
        """
        (self.clone / "genuine.txt").write_text("genuine local content\n")
        _run_git("add", "genuine.txt", cwd=self.clone)
        _run_git("commit", "-q", "-m", "docs: update skills", cwd=self.clone)
        genuine_sha = _git_out("rev-parse", "HEAD", cwd=self.clone)
        # Upstream has an UNRELATED commit with the SAME subject, different content.
        self._advance_upstream(
            subject="docs: update skills (#999)", filename="upstream.txt", content="totally different\n"
        )

        blockers = content_equivalence_blockers(str(self.clone), "main", "origin/main")

        assert blockers, "subject-colliding genuine commit must be a content-equivalence blocker"
        assert genuine_sha in blockers
        assert branch_content_upstream(str(self.clone), "main", "origin/main") is False

    def test_genuinely_upstreamed_branch_has_no_blockers(self) -> None:
        """A commit whose patch already landed upstream (real squash-merge) is proven upstream."""
        (self.clone / "feature.txt").write_text("the feature\n")
        _run_git("add", "feature.txt", cwd=self.clone)
        _run_git("commit", "-q", "-m", "add the feature", cwd=self.clone)
        # Upstream squash-merges the SAME patch (verbatim content) under a new sha.
        self._advance_upstream(subject="add the feature (#42)", filename="feature.txt", content="the feature\n")

        blockers = content_equivalence_blockers(str(self.clone), "main", "origin/main")

        assert blockers == [], "a patch-equivalent (squash-merged) commit must not block"
        assert branch_content_upstream(str(self.clone), "main", "origin/main") is True

    def test_merge_commit_in_range_is_a_blocker(self) -> None:
        """A merge commit (no single patch-id) conservatively blocks — it may carry unique content."""
        _run_git("checkout", "-q", "-b", "side", cwd=self.clone)
        (self.clone / "feature.txt").write_text("the feature\n")
        _run_git("add", "feature.txt", cwd=self.clone)
        _run_git("commit", "-q", "-m", "add the feature", cwd=self.clone)
        _run_git("checkout", "-q", "main", cwd=self.clone)
        _run_git("merge", "-q", "--no-ff", "--no-edit", "side", cwd=self.clone)
        # Upstream squash-merges only the feature subject, then is unrelated.
        self._advance_upstream(subject="add the feature (#42)", filename="feature.txt", content="the feature\n")

        blockers = content_equivalence_blockers(str(self.clone), "main", "origin/main")

        assert blockers, "a merge commit in the unique range must conservatively block"
        assert branch_content_upstream(str(self.clone), "main", "origin/main") is False

    def test_fails_closed_on_git_error(self) -> None:
        """An inconclusive content check (unresolvable target) REFUSES — never an empty pass."""
        blockers = content_equivalence_blockers(str(self.clone), "main", "origin/does-not-exist")

        assert blockers, "an inconclusive git probe must report a blocker, not an empty pass"
        assert any("inconclusive" in b for b in blockers)
        assert branch_content_upstream(str(self.clone), "main", "origin/does-not-exist") is False


class TestCleanAllRefusesSubjectCollision(TestCase):
    """The clean-all force-DELETE path must REQUIRE content-equivalence (#2609).

    The data-loss scenario: a branch with a GENUINE un-upstreamed commit whose
    subject collides with an upstream subject. ``classify_branch_commits`` drains
    it into ``squash_merged`` so ``genuinely_ahead`` is empty, and the branch is
    PUSHED to its own remote ref (so the #706 ``_raise_if_unpushed`` guard
    passes). Before this fix ``_raise_if_genuinely_ahead`` returned early on the
    empty ``genuinely_ahead`` and the worktree was force-deleted — destroying the
    genuine commit. The content gate must refuse.
    """

    @pytest.fixture(autouse=True)
    def _workspace(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        # A bare "origin" so the feature branch can be pushed (passes #706).
        self.bare = tmp_path / "origin.git"
        seed = tmp_path / "seed"
        _init_repo(seed)
        (seed / "base.txt").write_text("base\n")
        _run_git("add", "base.txt", cwd=seed)
        _run_git("commit", "-q", "-m", "initial", cwd=seed)
        _clone_repo(seed, self.bare, "--bare")
        self.repo_main = self.workspace / "myrepo"
        _clone_repo(self.bare, self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.repo_main)
        _run_git("config", "user.name", "t", cwd=self.repo_main)

    def _advance_origin(self, *, subject: str, filename: str, content: str) -> None:
        work = self.workspace.parent / f"adv-{filename}"
        _clone_repo(self.bare, work)
        _run_git("config", "user.email", "t@t", cwd=work)
        _run_git("config", "user.name", "t", cwd=work)
        (work / filename).write_text(content)
        _run_git("add", filename, cwd=work)
        _run_git("commit", "-q", "-m", subject, cwd=work)
        _run_git("push", "-q", "origin", "main", cwd=work)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)

    def _make_pushed_worktree(self, *, branch: str, subject: str, content: str) -> tuple[Worktree, Path, str]:
        wt_path = self.workspace / branch / "myrepo"
        _run_git("worktree", "add", "-q", "-b", branch, str(wt_path), cwd=self.repo_main)
        (wt_path / "genuine.txt").write_text(content)
        _run_git("add", "genuine.txt", cwd=wt_path)
        _run_git("commit", "-q", "-m", subject, cwd=wt_path)
        # Push the branch to its own remote ref so the #706 unpushed guard passes —
        # the work survives on the remote, but is genuinely NOT on origin/main.
        _run_git("push", "-q", "origin", f"{branch}:{branch}", cwd=wt_path)
        head = _git_out("rev-parse", "HEAD", cwd=wt_path)
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/2609",
            state=Ticket.State.IN_REVIEW,
        )
        worktree = Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch=branch,
            extra={"worktree_path": str(wt_path)},
        )
        return worktree, wt_path, head

    def _cleanup(self, worktree: Worktree) -> CleanupResult:
        with (
            patch("teatree.core.cleanup.clone_root", return_value=self.workspace),
            patch("teatree.core.cleanup.get_overlay_for_worktree") as mock_overlay,
        ):
            mock_overlay.return_value.get_cleanup_steps.return_value = []
            return cleanup_worktree(worktree, strict_hygiene=True)

    def test_refuses_to_force_delete_subject_colliding_genuine_work(self) -> None:
        """RED before #2609 — the genuine commit drains into squash_merged by subject and is destroyed.

        The local genuine commit's subject collides with an unrelated upstream
        subject (``docs: update skills``), so ``genuinely_ahead`` is empty and the
        old guard returned early → force-delete. The content gate must refuse.
        """
        worktree, wt_path, head = self._make_pushed_worktree(
            branch="2609-genuine", subject="docs: update skills", content="genuine un-upstreamed content\n"
        )
        # Upstream has the SAME canonicalized subject under DIFFERENT content.
        self._advance_origin(subject="docs: update skills (#999)", filename="other.txt", content="unrelated upstream\n")
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)

        with pytest.raises(RuntimeError, match="origin/main"):
            self._cleanup(worktree)

        # The worktree, branch, and the genuine commit all survive.
        assert wt_path.exists(), "worktree directory was destroyed despite genuine un-upstreamed work"
        assert head == _git_out("rev-parse", head, cwd=self.repo_main)
        assert Worktree.objects.filter(pk=worktree.pk).exists()

    def test_deletes_genuinely_upstreamed_branch(self) -> None:
        """A branch whose commit is content-equivalent upstream (real squash-merge) IS cleaned.

        Confirms the content gate does not over-block normal cleanup: when the
        local patch already landed verbatim on origin/main, the helper passes and
        the worktree is removed.
        """
        worktree, wt_path, _head = self._make_pushed_worktree(
            branch="2609-merged", subject="add the feature", content="the feature\n"
        )
        # Upstream squash-merges the SAME patch verbatim (the genuine.txt content).
        self._advance_origin(subject="add the feature (#42)", filename="genuine.txt", content="the feature\n")
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)

        result = self._cleanup(worktree)

        assert result.clean is True, f"unexpected errors cleaning a squash-merged branch: {result.errors}"
        assert not wt_path.exists(), "worktree should have been removed for a content-upstream branch"
        assert not Worktree.objects.filter(pk=worktree.pk).exists()
