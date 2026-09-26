"""The worktree-backlog reaper's data-loss path, under real git (souliane/teatree#219).

Every test here brackets one rung of the wipe invariant: ``cleanup_worktree(force=True)``
runs ONLY IF no pre-gate fired AND EITHER the checkout is RELEASABLE OR (the checked-out
branch is done AND every change is proven redundant AND the wipe fingerprint held). A pure
ghost row is never released by the sweep; that is ``workspace doctor --fix``'s job. Each
was watched RED under a named mutation of the production code before it was trusted green.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

from teatree.core.cleanup.cleanup import _EffectiveTarget
from teatree.core.cleanup.cleanup_liveness import LivenessVerdict
from teatree.core.models import Ticket, Worktree
from teatree.core.worktree import branch_classification, worktree_done
from teatree.core.worktree.branch_classification import branch_redundancy, reset_forge_probe_cache
from teatree.core.worktree.broken_checkout import is_pure_ghost
from teatree.core.worktree.worktree_done import reap_done_worktrees_detailed, worktree_is_done
from teatree.utils import git_run
from tests.teatree_core.cleanup._shared import _clean_env, _run_git, forge_reporting, squash_then_base_evolved
from tests.teatree_core.test_worktree_done import _ReaperFixture


class _DriftedBranchFixture(_ReaperFixture):
    """The (b) defect: the DB slug names one branch, the checkout holds another.

    ``_ReaperFixture`` leaves the row's slug and the checked-out branch identical, so
    the two probes agree by accident. These helpers pull them apart, which is the only
    state in which "which branch does done-detection judge?" is an answerable question.
    """

    def _land_on_main(self, filename: str, body: str) -> None:
        (self.repo_main / filename).write_text(body, encoding="utf-8")
        _run_git("add", "-A", cwd=self.repo_main)
        _run_git("commit", "-q", "-m", f"feat: land {filename}", cwd=self.repo_main)
        _run_git("push", "-q", "origin", "main", cwd=self.repo_main)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)

    def _checkout_new_branch(self, name: str, filename: str, body: str) -> None:
        _run_git("checkout", "-q", "-b", name, "main", cwd=self.wt_path)
        (self.wt_path / filename).write_text(body, encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", f"feat: land {filename}", cwd=self.wt_path)


class TestDoneSignalJudgesTheCheckedOutBranch(_DriftedBranchFixture):
    """T5 — done-detection reads the branch the checkout actually holds, not the slug.

    Mutation watched RED: ``worktree_is_done`` reverted to probing ``worktree.branch``.
    """

    def test_an_unmerged_checkout_on_a_landed_slug_is_never_wiped(self) -> None:
        # The wipe this prevents: the slug landed, so done-detection reading it says
        # done — while the checkout holds a pushed, unmerged, in-flight branch whose
        # commits no unpushed-work probe can object to.
        self._land_on_main("feat.txt", "feature work\n")
        self._checkout_new_branch("real-work", "real.txt", "in flight\n")
        _run_git("push", "-q", "origin", "real-work", cwd=self.wt_path)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)

        outcome = self._reap(self._make_worktree(Ticket.State.STARTED))

        assert outcome.action == "kept", outcome.label
        assert "not done" in outcome.label

    def test_a_landed_checkout_is_reaped_though_the_slug_never_landed(self) -> None:
        self._checkout_new_branch("real-work", "real.txt", "real work\n")
        self._land_on_main("real.txt", "real work\n")

        outcome = self._reap(self._make_worktree(Ticket.State.STARTED), dry_run=True)

        assert outcome.action == "would-wipe", outcome.label

    def test_the_done_signal_reads_the_branch_it_is_handed(self) -> None:
        self._land_on_main("feat.txt", "feature work\n")
        self._checkout_new_branch("real-work", "real.txt", "in flight\n")
        worktree = self._make_worktree(Ticket.State.STARTED)

        assert worktree_is_done(worktree, branch="feat-x").done
        assert not worktree_is_done(worktree, branch="real-work").done


class TestPureGhostRelease(_ReaperFixture):
    """T6 — the pure-ghost predicate: dir, branch ref and registration all proven gone.

    The predicate is ``workspace doctor --fix``'s; the automatic sweep never releases a ghost.
    """

    def _make_ghost(self) -> Worktree:
        worktree = self._make_worktree(Ticket.State.STARTED)
        _run_git("worktree", "remove", "--force", str(self.wt_path), cwd=self.repo_main)
        _run_git("update-ref", "-d", f"refs/heads/{self.slug}", cwd=self.repo_main)
        return worktree

    def test_the_sweep_never_releases_a_ghost_row(self) -> None:
        # Releasing a pure ghost is `workspace doctor --fix`'s job; the automatic sweep keeps it.
        worktree = self._make_ghost()
        assert is_pure_ghost(worktree, workspace=self.workspace)

        outcome = self._reap(worktree)

        assert outcome.action != "wiped", outcome.label
        assert Worktree.objects.filter(pk=worktree.pk).exists()

    def test_a_surviving_branch_ref_keeps_the_row(self) -> None:
        worktree = self._make_worktree(Ticket.State.STARTED)
        _run_git("worktree", "remove", "--force", str(self.wt_path), cwd=self.repo_main)

        assert not is_pure_ghost(worktree, workspace=self.workspace)
        assert self._reap(worktree).action != "wiped"

    def test_a_ghost_whose_clone_resolves_by_name_is_released(self) -> None:
        # The same resolution ``workspace doctor --fix`` tears ghosts down on.
        worktree = self._make_ghost()
        worktree.extra = {**(worktree.extra or {}), "clone_path": ""}
        worktree.save(update_fields=["extra"])

        assert is_pure_ghost(worktree, workspace=self.workspace)

    def test_a_name_scan_matching_two_clones_keeps_the_ghost(self) -> None:
        # The ref is missing from the clone a scan picks first and lives in the other.
        for namespace, with_branch in (("a", False), ("b", True)):
            clone = self.workspace / namespace / "tool"
            clone.mkdir(parents=True)
            _run_git("init", "-q", "-b", "main", cwd=clone)
            _run_git("config", "user.email", "t@t", cwd=clone)
            _run_git("config", "user.name", "t", cwd=clone)
            _run_git("commit", "--allow-empty", "-q", "-m", "init", cwd=clone)
            if with_branch:
                _run_git("branch", "live-work", cwd=clone)
        ghost = Worktree.objects.create(
            overlay="test",
            ticket=Ticket.objects.create(issue_url="https://example.com/issues/1", state=Ticket.State.STARTED),
            repo_path="tool",
            branch="live-work",
            extra={"worktree_path": str(self.workspace / "live-work" / "tool")},
        )

        assert not is_pure_ghost(ghost, workspace=self.workspace)

    def test_a_ref_probe_that_fails_keeps_the_ghost(self) -> None:
        worktree = self._make_ghost()
        real = git_run.run_allowed_to_fail

        def show_ref_fails(cmd: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if "show-ref" in cmd:
                return subprocess.CompletedProcess(cmd, 128, "", "fatal: bad object")
            return real(cmd, *args, **kwargs)

        with patch.object(git_run, "run_allowed_to_fail", side_effect=show_ref_fails):
            assert not is_pure_ghost(worktree, workspace=self.workspace)

    def test_a_still_registered_checkout_is_not_a_ghost(self) -> None:
        worktree = self._make_worktree(Ticket.State.STARTED)
        _run_git("update-ref", "-d", f"refs/heads/{self.slug}", cwd=self.repo_main)
        subprocess.run(  # the dir, not the registration, is what goes
            ["/bin/rm", "-rf", str(self.wt_path)], check=True, capture_output=True, env=_clean_env()
        )

        assert not is_pure_ghost(worktree, workspace=self.workspace)

    def test_a_busy_ticket_keeps_the_ghost_row(self) -> None:
        # The pre-gates run FIRST: a live ticket protects even a row with nothing on disk.
        worktree = self._make_ghost()
        with patch(
            "teatree.core.cleanup.reap_pre_gates.worktree_liveness",
            return_value=LivenessVerdict(active=True, reason="ticket has a live session"),
        ):
            outcome = self._reap(worktree)

        assert outcome.action == "active", outcome.label


class _WipeSpyFixture(_ReaperFixture):
    """Watches the ONE destructive call, so "was it reaped?" is answered by the wipe itself."""

    def _reap_watching_the_wipe(self, worktree: Worktree) -> tuple[object, list[object]]:
        wipes: list[object] = []
        real = worktree_done.cleanup_worktree

        def spy(row: Worktree, **kwargs: object) -> object:
            wipes.append(row)
            return real(row, **kwargs)

        with patch.object(worktree_done, "cleanup_worktree", side_effect=spy):
            outcome = self._reap(worktree)
        return outcome, wipes


class TestSquashMergedIsReaped(_WipeSpyFixture):
    """T1 — a squash-merge only the synthetic-squash rung can see is still reaped.

    Mutation watched RED: ``_content_redundancy``'s ``_tree_delta_captured`` rung
    disabled. Without this the ladder could be gutted and every other test here —
    all of which assert a KEEP — would stay green.

    The rung is asserted BY NAME as well as by outcome: the ladder is deliberately
    redundant, so the path-independent blob rung below also clears this branch and
    a disabled synthetic-squash rung is invisible in the reap outcome alone.
    """

    def _squash_onto_main(self) -> None:
        (self.wt_path / "feat.txt").write_text("feature work\nand more\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.wt_path)
        _run_git("commit", "-q", "-m", "feat: more of the feature", cwd=self.wt_path)
        (self.repo_main / "feat.txt").write_text("feature work\nand more\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.repo_main)
        _run_git("commit", "-q", "-m", "feat: ship the feature (#7)", cwd=self.repo_main)
        _run_git("push", "-q", "origin", "main", cwd=self.repo_main)
        _run_git("fetch", "-q", "origin", cwd=self.repo_main)

    def test_a_two_commit_branch_squashed_onto_main_is_wiped(self) -> None:
        self._squash_onto_main()

        outcome, wipes = self._reap_watching_the_wipe(self._make_worktree(Ticket.State.STARTED))

        assert outcome.action == "wiped", outcome.label
        assert len(wipes) == 1

    def test_the_synthetic_squash_rung_is_what_decides(self) -> None:
        self._squash_onto_main()

        verdict = branch_redundancy(str(self.repo_main), self.slug, "origin/main")

        assert verdict.redundant
        assert verdict.source == "synthetic-squash", f"a per-commit patch-id test cannot see this squash — {verdict}"


class TestUniqueContentIsNeverReaped(_WipeSpyFixture):
    """T2 — a branch holding content on no remote is kept, wipe never called.

    Mutation watched RED: ``content_equivalence_blockers`` stubbed to ``return []``.
    """

    def test_a_done_ticket_does_not_authorise_wiping_unique_work(self) -> None:
        outcome, wipes = self._reap_watching_the_wipe(self._make_worktree(Ticket.State.MERGED))

        assert outcome.action == "kept", outcome.label
        assert wipes == [], "the #706 guard must reach the wipe call, not merely the label"

    def test_the_kept_record_carries_the_unique_sha(self) -> None:
        outcome, _wipes = self._reap_watching_the_wipe(self._make_worktree(Ticket.State.MERGED))

        assert outcome.emit is not None
        assert outcome.emit.unique_commit_shas, "the delta a salvage would route must be named"


class TestBaseEvolvedNeedsTheForgeRecordAtTheExactTip:
    """T3 — the one rung no git-local probe can replace, and its two failure shapes.

    Mutation watched RED: ``forge_merged_tip_captured`` relaxed to ``return
    bool(recorded)`` — i.e. a bare merged signal, ignoring WHICH tip merged.
    """

    def _target(self, work: Path) -> _EffectiveTarget:
        return _EffectiveTarget(ref="feature", probe_repo=str(work), branch_to_delete="feature", label="feature")

    def _reasons(self, work: Path) -> list[str]:
        reset_forge_probe_cache()
        return worktree_done._unpushed_commit_reasons(work, self._target(work), default_target="origin/main")

    def test_a_merge_record_at_the_exact_tip_proves_it_landed(self, tmp_path: Path) -> None:
        work, tip = squash_then_base_evolved(tmp_path)

        with forge_reporting(merged_head_sha=tip):
            assert self._reasons(work) == []

    def test_a_merge_record_at_an_older_tip_keeps_the_post_merge_work(self, tmp_path: Path) -> None:
        work, tip = squash_then_base_evolved(tmp_path)
        _run_git("checkout", "-q", "feature", cwd=work)
        (work / "after.txt").write_text("written after the merge\n", encoding="utf-8")
        _run_git("add", "-A", cwd=work)
        _run_git("commit", "-q", "-m", "feat: post-merge work", cwd=work)

        with forge_reporting(merged_head_sha=tip):
            assert self._reasons(work), "the commit added after the merge is new work bound for a fresh PR"

    def test_no_forge_record_keeps_the_branch(self, tmp_path: Path) -> None:
        work, _tip = squash_then_base_evolved(tmp_path)

        with forge_reporting():
            assert self._reasons(work), "no instrument can see it landed — keep it"


class TestAGitErrorNeverReaps(_WipeSpyFixture):
    """T4 — an unreadable content probe keeps the row and says so; wipe never called.

    Mutation watched RED: ``content_equivalence_blockers`` swallowing
    ``CommandFailedError`` as ``return []`` instead of a blocker.
    """

    def _break_the_content_probe(self) -> None:
        _run_git("update-ref", "-d", "refs/remotes/origin/main", cwd=self.repo_main)

    def test_an_unreadable_probe_keeps_the_row_without_wiping(self) -> None:
        self._break_the_content_probe()

        outcome, wipes = self._reap_watching_the_wipe(self._make_worktree(Ticket.State.MERGED))

        assert outcome.action == "kept", outcome.label
        assert wipes == []

    def test_the_emitted_record_reports_the_verdict_as_unproven(self) -> None:
        self._break_the_content_probe()

        outcome, _wipes = self._reap_watching_the_wipe(self._make_worktree(Ticket.State.MERGED))

        assert outcome.emit is not None
        assert outcome.emit.verdict_source == "inconclusive"
        assert outcome.emit.content_verified is False


class TestLadderRunsOncePerRow(_ReaperFixture):
    """T9 — the landed ladder runs once per row, and its forge probes memoise.

    RED on the pre-change code, which ran the whole ladder from three consumers.
    """

    def _count_ladder_runs(self, worktree: Worktree) -> int:
        # Counted at ``_content_redundancy``, the one layer EVERY ``branch_redundancy``
        # call reaches: the consumers import the ladder into three module namespaces, so
        # patching any one of them counts a third of the runs and reads clean either way.
        reset_forge_probe_cache()
        real = branch_classification._content_redundancy
        runs: list[str] = []

        def counted(repo: str, branch: str, target: str) -> branch_classification.RedundancyVerdict:
            runs.append(branch)
            return real(repo, branch, target)

        with patch.object(branch_classification, "_content_redundancy", side_effect=counted):
            self._reap(worktree, dry_run=True)
        return len(runs)

    def test_a_kept_row_runs_the_ladder_once(self) -> None:
        ladder_runs = self._count_ladder_runs(self._make_worktree(Ticket.State.STARTED))

        assert ladder_runs == 1, f"the ladder ran {ladder_runs} times for one row"

    def test_a_done_row_runs_the_ladder_once(self) -> None:
        ladder_runs = self._count_ladder_runs(self._make_worktree(Ticket.State.MERGED))

        assert ladder_runs == 1, f"the ladder ran {ladder_runs} times for one row"

    def test_the_merge_commit_probe_memoises_within_a_pass(self) -> None:
        reset_forge_probe_cache()
        calls: list[str] = []

        with patch.object(branch_classification, "probe_host_cli", side_effect=lambda *a, **k: calls.append(1) or ""):
            branch_classification._pr_merge_commit_sha(str(self.repo_main), self.slug)
            first = len(calls)
            branch_classification._pr_merge_commit_sha(str(self.repo_main), self.slug)

        assert len(calls) == first, "a repeated probe for the same (repo, branch) must be served from the memo"

    def test_resetting_the_cache_re_probes(self) -> None:
        reset_forge_probe_cache()
        calls: list[int] = []

        with patch.object(branch_classification, "probe_host_cli", side_effect=lambda *a, **k: calls.append(1) or ""):
            branch_classification._pr_merge_commit_sha(str(self.repo_main), self.slug)
            reset_forge_probe_cache()
            branch_classification._pr_merge_commit_sha(str(self.repo_main), self.slug)

        assert len(calls) > 1, "a new pass must not answer from the previous pass's memo"


_BOOM = "boom"


class TestOneRaisingRowDoesNotAbortTheSweep(_ReaperFixture):
    """T8 — a row that raises is reported and skipped; the sweep continues.

    Mutation watched RED: remove the ``try`` from ``reap_done_worktrees_detailed``.
    """

    def _second_row(self) -> Worktree:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/2762", state=Ticket.State.STARTED)
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="myrepo",
            branch="feat-y",
            extra={"worktree_path": str(self.wt_path), "clone_path": str(self.repo_main)},
        )

    def test_a_raising_row_is_isolated_from_its_siblings(self) -> None:
        self._make_worktree(Ticket.State.STARTED)
        self._second_row()
        calls: list[Worktree] = []

        def explode_on_first(worktree: Worktree, **kwargs: object) -> worktree_done.ReapOutcome:
            calls.append(worktree)
            if len(calls) == 1:
                raise RuntimeError(_BOOM)
            return worktree_done.ReapOutcome("kept", f"KEPT '{worktree.branch}'")

        with patch.object(worktree_done, "reap_done_worktree", side_effect=explode_on_first):
            outcomes = reap_done_worktrees_detailed(self.workspace, dry_run=True)

        assert [o.action for o in outcomes] == ["error", "kept"]
        assert "RuntimeError('boom')" in outcomes[0].label
        assert "nothing wiped" in outcomes[0].label
