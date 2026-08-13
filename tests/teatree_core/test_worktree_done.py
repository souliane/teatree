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
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.cleanup.cleanup_liveness import LivenessVerdict, worktree_liveness
from teatree.core.models import Session, Ticket, UnshippedWorkRecord, Worktree
from teatree.core.runners import worktree_start
from teatree.core.worktree.branch_classification import RedundancyVerdict
from teatree.core.worktree.worktree_done import (
    ChangeAnalysis,
    _effective_default_target,
    _verdict_provenance,
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
        monkeypatch.setattr("teatree.core.cleanup.cleanup.clone_root", lambda: self.workspace)
        monkeypatch.setattr("teatree.core.worktree.worktree_done.clone_root", lambda: self.workspace)
        monkeypatch.setattr("teatree.core.cleanup.cleanup._resolve_overlay_or_none", lambda _wt: None)
        self.docker_calls: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            "teatree.core.runners.worktree_start.docker_compose_down",
            lambda project, **kw: self.docker_calls.append((project, bool(kw.get("remove_volumes")))),
        )
        # Neutralise the forge so the patch-id / FSM signals are the only deciders.
        monkeypatch.setattr("teatree.core.worktree.branch_classification.probe_host_cli", lambda *_a, **_k: "")
        # These tests model SETTLED worktrees (cleanup's target), not live ones; the
        # liveness guard has its own dedicated tests, so neutralise it here.
        monkeypatch.setattr(
            "teatree.core.cleanup.reap_pre_gates.worktree_liveness",
            lambda *_a, **_k: LivenessVerdict(active=False),
        )

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


class TestDanglingHeadUncommittedKept(_ReaperFixture):
    """A dangling-HEAD worktree (branch ref deleted post-merge) with real uncommitted edits is KEPT.

    With no resolvable HEAD, ``git status`` reports every tracked file as a staged
    add — noise. The pre-fix code SKIPPED the dirt check entirely there and let the
    recovered-HEAD commit analysis (all commits redundant on origin) decide WIPE,
    destroying the unexamined uncommitted follow-up edit. The working tree is now
    diffed against the recovered last-HEAD SHA + an untracked scan, so genuine
    uncommitted work keeps the worktree.
    """

    def test_dangling_head_with_uncommitted_edit_is_kept(self) -> None:
        self._push_branch()  # every commit is redundant on origin/feat-x…
        self._drop_local_branch_ref()  # …and HEAD is now a dangling symref (rc=128)
        (self.wt_path / "wip.txt").write_text("uncommitted follow-up, on no remote\n", encoding="utf-8")
        worktree = self._make_worktree(Ticket.State.MERGED)

        outcome = self._reap(worktree)

        assert outcome.action == "kept", outcome.label
        assert "uncommitted change" in outcome.label
        assert self.wt_path.exists(), "a dangling-HEAD worktree with real uncommitted edits must not be wiped"


class TestReapTocTouGuard(_ReaperFixture):
    """A commit landing between the redundancy analysis and the force-wipe is not destroyed.

    ``reap_done_worktree`` proves redundancy, then ``cleanup_worktree(force=True)``
    bypasses every data-loss guard. HEAD is re-read just before the wipe and the
    wipe refused if it moved, so a commit that lands in the TOCTOU window survives.
    """

    def test_commit_landing_in_the_window_keeps_worktree(self) -> None:
        self._push_branch()
        worktree = self._make_worktree(Ticket.State.MERGED)

        def racing_analyze(wt: Worktree, *, workspace: Path) -> ChangeAnalysis:
            # A new commit lands AFTER head_at_analysis was captured, BEFORE the wipe.
            (self.wt_path / "late.txt").write_text("late-landing work\n", encoding="utf-8")
            _run_git("add", "-A", cwd=self.wt_path)
            _run_git("commit", "-q", "-m", "feat: late landing", cwd=self.wt_path)
            return ChangeAnalysis(proven_redundant=True)

        with patch(
            "teatree.core.worktree.worktree_done.analyze_worktree_changes",
            side_effect=racing_analyze,
        ):
            outcome = self._reap(worktree)

        assert outcome.action == "kept", outcome.label
        assert "HEAD moved" in outcome.label
        assert self.wt_path.exists(), "a commit landing during analysis must not be force-wiped"


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


class TestOpenPrRefusesSquashMergedDone(_ReaperFixture):
    """A branch whose content matches origin/main but whose PR is still OPEN is NOT done (#3093).

    The squash-merged content heuristic (patch-id ``git cherry``) matches whenever a
    branch's current tip is content-equivalent to ``origin/main`` — including a branch
    whose PR is still OPEN and merely resembles the default branch. Reporting such a
    worktree ``done (squash-merged)`` is a false-done a sweep can act on to wipe live
    work. An open PR on the forge is positive proof the work is unfinished, so ``done``
    must be refused before the content heuristic is trusted.
    """

    def _land_branch_content_on_main(self) -> None:
        _run_git("merge", "-q", "--squash", self.slug, cwd=self.repo_main)
        _run_git("commit", "-q", "-m", "squash: ship the feature (#2761)", cwd=self.repo_main)
        _run_git("push", "-q", "origin", "main", cwd=self.repo_main)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)

    def test_open_pr_refuses_squash_merged_done(self) -> None:
        self._land_branch_content_on_main()  # tip is now patch-id present on origin/main
        worktree = self._make_worktree(Ticket.State.STARTED)  # FSM not terminal → squash path decides

        # Sanity: with no open-PR signal the content heuristic DOES classify it done.
        assert worktree_is_done(worktree).source == "squash-merged"

        # The forge reports an OPEN PR for the branch → done must be refused.
        with patch("teatree.core.worktree.branch_classification.probe_host_cli", return_value="7"):
            signal = worktree_is_done(worktree)
            outcome = self._reap(worktree)

        assert signal.done is False, signal.source
        assert outcome.action == "kept", outcome.label
        assert self.wt_path.exists(), "a worktree backing an OPEN PR must never be wiped"


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
        with patch("teatree.core.worktree.branch_classification.probe_host_cli", return_value="42"):
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
        with patch("teatree.core.cleanup.reap_pre_gates.is_clean_ignored", return_value=True):
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
            "teatree.core.cleanup.reap_pre_gates.worktree_liveness",
            return_value=LivenessVerdict(active=True, reason="ticket has a live session or active/claimed task"),
        ):
            outcome = self._reap(worktree)

        assert outcome.action == "active", outcome.label
        assert "live session" in outcome.label
        assert self.wt_path.exists(), "a live item must never be wiped"
        assert outcome.emit is not None
        assert outcome.emit.liveness

    def test_colleague_authored_item_is_excluded(self) -> None:
        from teatree.core.cleanup.cleanup_ownership import OwnershipVerdict  # noqa: PLC0415

        worktree = self._make_worktree(Ticket.State.MERGED)
        with patch(
            "teatree.core.cleanup.reap_pre_gates.is_excluded_by_ownership",
            return_value=OwnershipVerdict(excluded=True, reason="colleague-authored (bob) on a product repo"),
        ):
            outcome = self._reap(worktree)

        assert outcome.action == "excluded", outcome.label
        assert "colleague-authored" in outcome.label
        assert self.wt_path.exists(), "a colleague's work must never be wiped"
        assert outcome.emit is not None


class TestStaleSessionReachesTheDonePath(_ReaperFixture):
    """End-to-end: an abandoned open Session must stop returning ACTIVE before done-detection.

    The consequence chain the session-close defect produced — never-written
    ``ended_at`` → ``has_active_work`` permanently true → ``reap_pre_gate``
    returning ACTIVE ahead of done-detection → ``clean-all`` never converging.
    These run the REAL liveness guard (the fixture's stub is restored per test).
    """

    def _reap_with_real_liveness(self, worktree: Worktree) -> object:
        with patch("teatree.core.cleanup.reap_pre_gates.worktree_liveness", worktree_liveness):
            return self._reap(worktree)

    def _backdate_head(self) -> None:
        """Age HEAD past the recent-commit window so the SESSION signal is the decider."""
        stamp = "2020-01-01T00:00:00 +0000"
        env = {**_clean_env(), "GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_DATE": stamp}
        subprocess.run(
            [_GIT, "-C", str(self.wt_path), "commit", "-q", "--amend", "--no-edit"],
            check=True,
            capture_output=True,
            env=env,
        )

    def test_merged_ticket_with_a_stale_session_is_wiped(self) -> None:
        self._backdate_head()
        self._push_branch()
        worktree = self._make_worktree(Ticket.State.MERGED)
        session = Session.objects.create(overlay="test", ticket=worktree.ticket)
        Session.objects.filter(pk=session.pk).update(started_at=timezone.now() - timedelta(days=7))

        outcome = self._reap_with_real_liveness(worktree)

        assert outcome.action == "wiped", outcome.label
        assert not self.wt_path.exists()

    def test_merged_ticket_with_a_recent_session_is_still_skipped(self) -> None:
        """The fail-CLOSED control: a genuinely live session keeps the worktree."""
        self._backdate_head()
        self._push_branch()
        worktree = self._make_worktree(Ticket.State.MERGED)
        Session.objects.create(overlay="test", ticket=worktree.ticket)

        outcome = self._reap_with_real_liveness(worktree)

        assert outcome.action == "active", outcome.label
        assert self.wt_path.exists()


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

        with patch("teatree.core.worktree.branch_classification.probe_host_cli", return_value="42"):
            outcome = self._reap(worktree)

        assert outcome.action == "kept", outcome.label
        assert outcome.emit is not None
        assert outcome.emit.merged_with_post_merge_work is True
        assert outcome.emit.unique_commit_shas, "post-merge SHAs must be emitted for a fresh PR"


class TestEmitVerdictProvenance(_ReaperFixture):
    """An empty ``unique_commit_shas`` must never mean two opposite things.

    A probe that could not run and a tip proven to hold nothing unique both leave
    the list empty; only ``content_verified`` tells the judgment skill which one it
    is looking at, and only the second may be routed to DELETE.
    """

    def _make_unresolvable_clone(self, state: str) -> Worktree:
        """A row whose clone cannot be found by NAME and whose checkout is gone.

        Both halves are needed. A stale ``clone_path`` alone is no longer
        unresolvable: the resolver asks git which clone the CHECKOUT belongs to,
        and an on-disk worktree answers. Genuinely unresolvable means there is
        nothing left to ask — no name match and no checkout.
        """
        worktree = self._make_worktree(state)
        worktree.repo_path = "ghostrepo"
        worktree.extra = {
            **worktree.extra,
            "clone_path": str(self.tmp_path / "moved-away" / "ghostrepo"),
            "worktree_path": str(self.tmp_path / "moved-away" / "gone-checkout"),
        }
        worktree.save(update_fields=["repo_path", "extra"])
        return worktree

    def test_an_unresolvable_clone_emits_an_unverified_record(self) -> None:
        outcome = self._reap(self._make_unresolvable_clone(Ticket.State.STARTED))

        assert outcome.action == "kept", outcome.label
        assert outcome.emit is not None
        assert outcome.emit.unique_commit_shas == [], "no probe ran, so no commit can be named"
        assert outcome.emit.content_verified is False
        assert outcome.emit.verdict_source == "clone-unresolvable"

    def test_a_stale_clone_path_over_a_live_checkout_is_now_verifiable(self) -> None:
        """The recovered case: the stored path moved away, but git still knows the clone.

        This is the bulk of what used to emit as ``clone-unresolvable`` — a row
        whose recorded clone path went stale while the worktree itself sat right
        there. It emitted an empty commit list with ``content_verified: false``,
        which reads identically to a branch proven to hold nothing.
        """
        worktree = self._make_worktree(Ticket.State.STARTED)
        worktree.repo_path = "ghostrepo"
        worktree.extra = {**worktree.extra, "clone_path": str(self.tmp_path / "moved-away" / "ghostrepo")}
        worktree.save(update_fields=["repo_path", "extra"])

        outcome = self._reap(worktree)

        assert outcome.emit is not None
        assert outcome.emit.content_verified is True
        assert outcome.emit.verdict_source != "clone-unresolvable"

    def test_a_proven_redundant_record_still_emits_the_deletable_shape(self) -> None:
        _run_git("merge", "-q", "--squash", self.slug, cwd=self.repo_main)  # the tip's content ships…
        _run_git("commit", "-q", "-m", "squash: ship the feature (#2761)", cwd=self.repo_main)
        _run_git("push", "-q", "origin", "main", cwd=self.repo_main)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)
        (self.wt_path / "wip.txt").write_text("uncommitted work in progress\n", encoding="utf-8")  # …this keeps it
        worktree = self._make_worktree(Ticket.State.MERGED)

        outcome = self._reap(worktree)

        assert outcome.action == "kept", outcome.label
        assert outcome.emit is not None
        assert outcome.emit.unique_commit_shas == []
        assert outcome.emit.content_verified is True
        assert outcome.emit.verdict_source == "cherry-zero-unique"


def test_inconclusive_verdict_over_a_real_clone_is_not_verified(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([_GIT, "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True, env=_clean_env())

    inconclusive = RedundancyVerdict(redundant=False, forge_merged=False, source="inconclusive")

    assert _verdict_provenance(repo, inconclusive) == (False, "inconclusive")
    assert _verdict_provenance(repo, RedundancyVerdict(redundant=False, forge_merged=False)) == (True, "not-redundant")
    assert _verdict_provenance(tmp_path / "absent", inconclusive) == (False, "clone-unresolvable")


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

        monkeypatch.setattr("teatree.core.worktree.worktree_done.clone_root", lambda: self.workspace)
        monkeypatch.setattr("teatree.core.worktree.branch_classification.probe_host_cli", lambda *_a, **_k: "")

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


class TestCaptureCoversEveryDisposition(_ReaperFixture):
    """The sweep observes a KEPT row's work, not only a torn-down one's (#4272).

    The capture ran solely inside teardown, so the one disposition that never
    tears anything down — a row the sweep KEEPS because its ticket is open —
    wrote no record, and the surfacing half (#3891) had nothing to age. Measured
    on the host that filed the ticket: 75 of 77 registered rows uncaptured, the
    worst holding 25 modified files with no commit and no remote branch.
    """

    @pytest.fixture(autouse=True)
    def _capture_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.core.cleanup.unshipped_work.get_data_dir", lambda *_a, **_k: tmp_path / "captures")

    def test_a_kept_open_ticket_row_is_captured_so_the_doctor_can_age_it(self) -> None:
        (self.wt_path / "stranded.txt").write_text("exists on one disk and nowhere else\n", encoding="utf-8")
        worktree = self._make_worktree(Ticket.State.STARTED)

        outcome = self._reap(worktree)

        assert outcome.action == "kept", outcome.label
        record = UnshippedWorkRecord.objects.get(checkout_path=str(self.wt_path))
        assert "stranded.txt" in record.dirty_paths

    def test_an_active_row_the_pre_gate_skips_is_captured_before_the_gate_decides(self) -> None:
        (self.wt_path / "stranded.txt").write_text("live, and still unshipped\n", encoding="utf-8")
        worktree = self._make_worktree(Ticket.State.STARTED)

        with patch(
            "teatree.core.cleanup.reap_pre_gates.worktree_liveness",
            return_value=LivenessVerdict(active=True, reason="a session is live in it"),
        ):
            outcome = self._reap(worktree)

        assert outcome.action == "active", outcome.label
        assert UnshippedWorkRecord.objects.filter(checkout_path=str(self.wt_path)).exists()

    def test_a_dry_run_preview_records_nothing(self) -> None:
        (self.wt_path / "stranded.txt").write_text("preview must stay side-effect free\n", encoding="utf-8")
        worktree = self._make_worktree(Ticket.State.STARTED)

        self._reap(worktree, dry_run=True)

        assert not UnshippedWorkRecord.objects.exists()


def test_effective_default_target_failsafe_to_main_on_unresolvable(tmp_path: Path) -> None:
    """An unresolvable default (the path is not a git repo) fails safe to origin/main.

    The downstream content gate fails CLOSED on a missing target (``git cherry``
    is inconclusive), so a wrong/missing base keeps the worktree rather than
    wiping it.
    """
    assert _effective_default_target(tmp_path / "not-a-repo") == "origin/main"
