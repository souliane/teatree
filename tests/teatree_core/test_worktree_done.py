"""The done-detection + analyze-before-wipe reaper, against real git under tmp_path.

These are the load-bearing regressions for the cleanup redesign:

- a MERGED ticket whose local branch ref was deleted is DONE via the FSM state
(no git probe), so it is wiped — the rc=128 fix that stranded ~76 worktrees;
- a STARTED ticket with a unique unpushed commit is NOT done, so it is KEPT, and
the removed snapshot path means NO ``t3-recover-*`` artifact is created anywhere;
- a done ticket whose worktree has a real uncommitted change is KEPT and reported
(the per-change analyze-before-wipe primary safety);
- a SHIPPED ticket (PR still open) is NOT done; the #706 guard keeps genuinely-ahead
unpushed work even on a done ticket; and the done-wipe tears the docker volumes down.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.cleanup_liveness import LivenessVerdict
from teatree.core.models import Ticket, Worktree
from teatree.core.runners import worktree_start
from teatree.core.worktree_done import (
    _effective_default_target,
    analyze_worktree_changes,
    reap_done_worktree,
    reap_done_worktrees,
    worktree_is_done,
)
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git


class _ReaperFixture(TestCase):
    """A real ``main`` clone + bare ``origin`` + one worktree on ``feat-x``.

    Subclasses (or individual tests) push/merge/dirty the worktree to model each
    disposition, then drive :func:`reap_done_worktree`. The forge probes are
    neutralised (no ``gh``/``glab`` in the loop), so the deterministic patch-id /
    FSM-state signals decide — never a network call.
    """

    slug = "feat-x"

    @pytest.fixture(autouse=True)
    def _tmp_workspace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()

        self.remote = tmp_path / "remote.git"
        subprocess.run(
            [_GIT, "init", "-q", "--bare", "-b", "main", str(self.remote)],
            check=True,
            capture_output=True,
            env=_clean_env(),
        )

        self.repo_main = self.workspace / "myrepo"
        self.repo_main.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.repo_main)
        _run_git("config", "user.name", "t", cwd=self.repo_main)
        _run_git("remote", "add", "origin", str(self.remote), cwd=self.repo_main)
        (self.repo_main / "base.txt").write_text("base\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.repo_main)
        _run_git("commit", "-q", "-m", "initial", cwd=self.repo_main)
        _run_git("push", "-q", "origin", "main", cwd=self.repo_main)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)

        self.wt_path = self.workspace / self.slug / "myrepo"
        _run_git("worktree", "add", "-q", "-b", self.slug, str(self.wt_path), cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.wt_path)
        _run_git("config", "user.name", "t", cwd=self.wt_path)
        (self.wt_path / "feat.txt").write_text("feature work\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "feat: ship the feature", cwd=self.wt_path)

        # No git worktree, DB, or docker is destroyed against a real overlay: route
        # cleanup through the overlay-free teardown and stub the docker side-effect.
        monkeypatch.setattr("teatree.core.cleanup.load_config", self._config)
        monkeypatch.setattr("teatree.core.worktree_done.load_config", self._config)
        monkeypatch.setattr("teatree.core.cleanup._resolve_overlay_or_none", lambda _wt: None)
        self.docker_calls: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            "teatree.core.runners.worktree_start.docker_compose_down",
            lambda project, **kw: self.docker_calls.append((project, bool(kw.get("remove_volumes")))),
        )
        # Neutralise the forge so the patch-id / FSM signals are the only deciders.
        monkeypatch.setattr("teatree.core.branch_classification.probe_host_cli", lambda *_a, **_k: "")
        # These tests model SETTLED worktrees (cleanup's target), not live ones; the
        # liveness guard has its own dedicated tests, so neutralise it here.
        monkeypatch.setattr(
            "teatree.core.worktree_done.worktree_liveness",
            lambda *_a, **_k: LivenessVerdict(active=False),
        )

    def _config(self) -> object:
        class _Cfg:
            class user:  # noqa: N801 — mirrors load_config().user.workspace_dir
                workspace_dir = self.workspace

        return _Cfg()

    def _make_worktree(self, state: str) -> Worktree:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/2761", state=state)
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch=self.slug,
            extra={"worktree_path": str(self.wt_path), "clone_path": str(self.repo_main)},
        )

    def _push_branch(self) -> None:
        _run_git("push", "-q", "origin", self.slug, cwd=self.wt_path)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)

    def _drop_local_branch_ref(self) -> None:
        _run_git("update-ref", "-d", f"refs/heads/{self.slug}", cwd=self.repo_main)

    def _reap(self, worktree: Worktree, *, dry_run: bool = False) -> object:
        return reap_done_worktree(worktree, workspace=self.workspace, dry_run=dry_run)


class TestMergedDeletedRefWiped(_ReaperFixture):
    """A MERGED ticket whose branch ref was deleted is DONE via FSM state — wiped."""

    def test_merged_ticket_with_deleted_branch_ref_is_wiped(self) -> None:
        self._push_branch()  # HEAD now contained in origin/feat-x
        self._drop_local_branch_ref()  # dangling HEAD — the rc=128 probe failure
        worktree = self._make_worktree(Ticket.State.MERGED)

        outcome = self._reap(worktree)

        assert outcome.action == "wiped", outcome.label
        assert "ticket-state:merged" in outcome.label
        assert not self.wt_path.exists(), "merged + deleted-ref worktree must be reaped (the 76-leak fix)"
        assert not Worktree.objects.filter(pk=worktree.pk).exists()

    def test_done_signal_reads_fsm_state_without_touching_git(self) -> None:
        self._drop_local_branch_ref()
        signal = worktree_is_done(self._make_worktree(Ticket.State.MERGED))
        assert signal.done is True
        assert signal.source == "ticket-state:merged"


class TestNotDoneUnpushedKeptNoSnapshot(_ReaperFixture):
    """A STARTED ticket with unique unpushed work is NOT done — KEPT, no snapshot."""

    def test_started_with_unique_unpushed_commit_is_kept(self) -> None:
        worktree = self._make_worktree(Ticket.State.STARTED)  # never pushed

        outcome = self._reap(worktree)

        assert outcome.action == "kept", outcome.label
        assert "not done" in outcome.label
        assert self.wt_path.exists(), "genuinely-unsynced work must never be destroyed"
        assert Worktree.objects.filter(pk=worktree.pk).exists()

    def test_no_recovery_snapshot_artifact_is_created_anywhere(self) -> None:
        self._reap(self._make_worktree(Ticket.State.STARTED))
        assert list(self.tmp_path.rglob("t3-recover-*")) == [], "the snapshot path is gone — no t3-recover-* dir"


class TestDoneButUncommittedKept(_ReaperFixture):
    """A done ticket whose worktree has a real uncommitted change is KEPT + reported."""

    def test_uncommitted_change_not_proven_redundant_keeps_worktree(self) -> None:
        self._push_branch()  # commits are redundant…
        (self.wt_path / "wip.txt").write_text("uncommitted work in progress\n", encoding="utf-8")  # …but this is not
        worktree = self._make_worktree(Ticket.State.MERGED)

        outcome = self._reap(worktree)

        assert outcome.action == "kept", outcome.label
        assert "uncommitted change" in outcome.label
        assert self.wt_path.exists()


class TestShippedIsNotDone(_ReaperFixture):
    """SHIPPED (PR still open) is NOT a done state — the worktree is kept."""

    def test_shipped_ticket_is_not_done(self) -> None:
        self._push_branch()
        worktree = self._make_worktree(Ticket.State.SHIPPED)

        signal = worktree_is_done(worktree)
        outcome = self._reap(worktree)

        assert signal.done is False
        assert signal.source == "not-done:shipped"
        assert outcome.action == "kept"
        assert self.wt_path.exists()


class Test706GuardKeepsGenuinelyAheadOnDoneTicket(_ReaperFixture):
    """Even on a MERGED ticket, genuinely-ahead unpushed work is KEPT (#706 / CORRECTION 1)."""

    def test_merged_ticket_with_unpushed_unique_commit_is_kept(self) -> None:
        worktree = self._make_worktree(Ticket.State.MERGED)  # commit never pushed anywhere

        analysis = analyze_worktree_changes(worktree, workspace=self.workspace)
        outcome = self._reap(worktree)

        assert analysis.proven_redundant is False
        assert any("not provably on origin/main" in r for r in analysis.kept_reasons)
        assert outcome.action == "kept", outcome.label
        assert self.wt_path.exists()


class TestMergedPrDoesNotWipePostMergeWork(_ReaperFixture):
    """A merged PR does NOT authorise wiping post-merge commits not on origin/main.

    The deletion gate is content-based on the CURRENT tip (CORRECTION 1: every
    change PROVEN redundant by patch-id), so a branch that shipped a PR and then
    grew NEW commits — content absent from origin/main — is KEPT for salvage to a
    fresh PR, never wiped on the stale forge-merged signal alone. Regression: the
    ``_branch_pr_is_merged`` short-circuit used to return "redundant" here even when
    the current tip carried unique post-merge content, destroying that work.
    """

    def _land_original_on_main_then_add_post_merge_commit(self) -> None:
        # Squash the branch's original commit onto origin/main (the PR merge), so its
        # content is patch-id-present upstream …
        _run_git("merge", "-q", "--squash", self.slug, cwd=self.repo_main)
        _run_git("commit", "-q", "-m", "squash: ship the feature (#2761)", cwd=self.repo_main)
        _run_git("push", "-q", "origin", "main", cwd=self.repo_main)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)
        # … then add NEW post-merge work whose content is NOT on origin/main.
        (self.wt_path / "post.txt").write_text("post-merge continued work\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "feat: continued work after the merge", cwd=self.wt_path)

    def test_post_merge_commit_with_merged_pr_is_kept(self) -> None:
        self._land_original_on_main_then_add_post_merge_commit()
        worktree = self._make_worktree(Ticket.State.MERGED)

        # The forge genuinely reports the branch merged (probe_host_cli yields a PR id),
        # so the REAL _branch_pr_is_merged returns True however it is imported — proving
        # the keep is content-on-current-tip, not the absence of a merged signal.
        with patch("teatree.core.branch_classification.probe_host_cli", return_value="42"):
            analysis = analyze_worktree_changes(worktree, workspace=self.workspace)
            outcome = self._reap(worktree)

        assert analysis.proven_redundant is False, analysis.kept_reasons
        assert any("not provably on origin/main" in r for r in analysis.kept_reasons)
        assert outcome.action == "kept", outcome.label
        assert self.wt_path.exists(), "post-merge work must never be wiped on a stale merged-PR signal"
        assert Worktree.objects.filter(pk=worktree.pk).exists()


class TestDoneWipeTearsDownDockerVolumes(_ReaperFixture):
    """The done-wipe runs ``docker compose down --volumes`` for the worktree's stack."""

    def test_wipe_invokes_docker_compose_down_with_volumes(self) -> None:
        self._push_branch()
        self._drop_local_branch_ref()
        worktree = self._make_worktree(Ticket.State.MERGED)

        self._reap(worktree)

        assert self.docker_calls, "the done-wipe must tear the worktree's docker stack down"
        assert all(remove_volumes for _project, remove_volumes in self.docker_calls), (
            "the done-wipe must pass remove_volumes=True so the worktree's docker volumes are reaped"
        )


def test_docker_compose_down_emits_volumes_flag_when_requested() -> None:
    """``docker compose down`` carries ``--volumes`` only when remove_volumes is set."""
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stderr = ""

    with patch.object(worktree_start, "run_allowed_to_fail", lambda cmd, **_kw: calls.append(cmd) or _Result()):
        worktree_start.docker_compose_down("proj", remove_volumes=True)
        worktree_start.docker_compose_down("proj")

    assert "--volumes" in calls[0]
    assert "--volumes" not in calls[1]


class TestDryRunAndCleanIgnore(_ReaperFixture):
    """--dry-run lists what would wipe without removing; clean_ignore is never reaped."""

    def test_dry_run_lists_without_removing(self) -> None:
        self._push_branch()
        self._drop_local_branch_ref()
        worktree = self._make_worktree(Ticket.State.MERGED)

        outcome = self._reap(worktree, dry_run=True)

        assert outcome.action == "would-wipe", outcome.label
        assert self.wt_path.exists(), "dry-run must not remove the worktree"
        assert Worktree.objects.filter(pk=worktree.pk).exists()

    def test_clean_ignored_branch_is_skipped(self) -> None:
        worktree = self._make_worktree(Ticket.State.MERGED)
        with patch("teatree.core.worktree_done.is_clean_ignored", return_value=True):
            outcome = self._reap(worktree)
        assert outcome.action == "skipped"
        assert self.wt_path.exists()

    def test_reap_done_worktrees_sweep_returns_one_line_per_row(self) -> None:
        self._push_branch()
        self._drop_local_branch_ref()
        self._make_worktree(Ticket.State.MERGED)

        lines = reap_done_worktrees(self.workspace, dry_run=True)

        assert len(lines) == 1
        assert lines[0].startswith("WOULD WIPE")


class TestReaperGatesAndEmit(_ReaperFixture):
    """The ownership/liveness pre-gates route correctly, and kept items carry an emit record."""

    def test_kept_item_carries_an_emit_record(self) -> None:
        worktree = self._make_worktree(Ticket.State.STARTED)  # not done → kept

        outcome = self._reap(worktree)

        assert outcome.action == "kept"
        assert outcome.emit is not None
        emit = outcome.emit
        assert emit.branch == self.slug
        assert emit.kind == "worktree"
        assert emit.unique_commit_shas, "the unique commit must be emitted for salvage"
        assert emit.banned_terms_status == "clean"
        assert emit.owner == "t"
        assert emit.last_commit_date, "the tip commit date must be emitted"
        assert emit.merged_with_post_merge_work is False

    def test_active_item_is_skipped_and_emitted(self) -> None:
        worktree = self._make_worktree(Ticket.State.MERGED)
        with patch(
            "teatree.core.worktree_done.worktree_liveness",
            return_value=LivenessVerdict(active=True, reason="ticket has a live session or active/claimed task"),
        ):
            outcome = self._reap(worktree)

        assert outcome.action == "active", outcome.label
        assert "live session" in outcome.label
        assert self.wt_path.exists(), "a live item must never be wiped"
        assert outcome.emit is not None
        assert outcome.emit.liveness

    def test_colleague_authored_item_is_excluded(self) -> None:
        from teatree.core.cleanup_ownership import OwnershipVerdict  # noqa: PLC0415

        worktree = self._make_worktree(Ticket.State.MERGED)
        with patch(
            "teatree.core.worktree_done.is_excluded_by_ownership",
            return_value=OwnershipVerdict(excluded=True, reason="colleague-authored (bob) on a product repo"),
        ):
            outcome = self._reap(worktree)

        assert outcome.action == "excluded", outcome.label
        assert "colleague-authored" in outcome.label
        assert self.wt_path.exists(), "a colleague's work must never be wiped"
        assert outcome.emit is not None


class TestPostMergeWorkEmitTag(_ReaperFixture):
    """A merged-PR branch with post-merge work is KEPT and emitted tagged merged_with_post_merge_work."""

    def test_post_merge_kept_item_emit_is_tagged(self) -> None:
        _run_git("merge", "-q", "--squash", self.slug, cwd=self.repo_main)
        _run_git("commit", "-q", "-m", "squash: ship the feature (#2761)", cwd=self.repo_main)
        _run_git("push", "-q", "origin", "main", cwd=self.repo_main)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)
        (self.wt_path / "post.txt").write_text("post-merge work\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "feat: continued after the merge", cwd=self.wt_path)
        worktree = self._make_worktree(Ticket.State.MERGED)

        with patch("teatree.core.branch_classification.probe_host_cli", return_value="42"):
            outcome = self._reap(worktree)

        assert outcome.action == "kept", outcome.label
        assert outcome.emit is not None
        assert outcome.emit.merged_with_post_merge_work is True
        assert outcome.emit.unique_commit_shas, "post-merge SHAs must be emitted for a fresh PR"


class TestSnapshotModulesRemoved:
    """The #1770 snapshot mechanism is gone — its modules no longer import."""

    def test_worktree_snapshot_module_is_removed(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            __import__("teatree.core.worktree_snapshot")

    def test_worktree_recovery_module_is_removed(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            __import__("teatree.core.worktree_recovery")


class TestNonMainDefaultThreading(TestCase):
    """N1: analyze resolves the repo's REAL default branch, never a hardcoded origin/main.

    Anti-vacuous: the worktree lives on a ``master``-default repo with a unique
    unpushed commit. The kept-reason must name ``origin/master`` — if the probe
    still compared against a hardcoded ``origin/main`` (a ref this repo does not
    have) the content gate would be inconclusive and the message would read
    ``origin/main``, so the assertion distinguishes the fix from the bug.
    """

    @pytest.fixture(autouse=True)
    def _master_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.remote = tmp_path / "remote.git"
        subprocess.run(
            [_GIT, "init", "-q", "--bare", "-b", "master", str(self.remote)],
            check=True,
            capture_output=True,
            env=_clean_env(),
        )
        self.repo_main = self.workspace / "myrepo"
        self.repo_main.mkdir()
        _run_git("init", "-q", "-b", "master", cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.repo_main)
        _run_git("config", "user.name", "t", cwd=self.repo_main)
        _run_git("remote", "add", "origin", str(self.remote), cwd=self.repo_main)
        (self.repo_main / "base.txt").write_text("base\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.repo_main)
        _run_git("commit", "-q", "-m", "initial", cwd=self.repo_main)
        _run_git("push", "-q", "origin", "master", cwd=self.repo_main)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)

        self.wt_path = self.workspace / "feat" / "myrepo"
        _run_git("worktree", "add", "-q", "-b", "feat", str(self.wt_path), cwd=self.repo_main)
        _run_git("config", "user.email", "t@t", cwd=self.wt_path)
        _run_git("config", "user.name", "t", cwd=self.wt_path)
        (self.wt_path / "feat.txt").write_text("unique work\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "feat: unique unpushed work", cwd=self.wt_path)

        monkeypatch.setattr("teatree.core.worktree_done.load_config", self._config)
        monkeypatch.setattr("teatree.core.branch_classification.probe_host_cli", lambda *_a, **_k: "")

    def _config(self) -> object:
        class _Cfg:
            class user:  # noqa: N801 — mirrors load_config().user.workspace_dir
                workspace_dir = self.workspace

        return _Cfg()

    def _worktree(self) -> Worktree:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/n1", state=Ticket.State.MERGED)
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch="feat",
            extra={"worktree_path": str(self.wt_path), "clone_path": str(self.repo_main)},
        )

    def test_effective_default_target_resolves_the_real_default(self) -> None:
        assert _effective_default_target(self.repo_main) == "origin/master"

    def test_unpushed_keep_reason_names_the_real_default_branch(self) -> None:
        analysis = analyze_worktree_changes(self._worktree(), workspace=self.workspace)
        assert analysis.proven_redundant is False
        assert any("origin/master" in r for r in analysis.kept_reasons), analysis.kept_reasons
        assert not any("origin/main" in r for r in analysis.kept_reasons), analysis.kept_reasons


def test_effective_default_target_failsafe_to_main_on_unresolvable(tmp_path: Path) -> None:
    """An unresolvable default (the path is not a git repo) fails safe to origin/main.

    The downstream content gate fails CLOSED on a missing target (``git cherry``
    is inconclusive), so a wrong/missing base keeps the worktree rather than
    wiping it.
    """
    assert _effective_default_target(tmp_path / "not-a-repo") == "origin/main"
