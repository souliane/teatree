"""``workspace emit`` must name work held in a worktree teatree never registered (#4579).

`emit` walked the ``Worktree`` ledger, so a checkout created by a dispatched agent's bare
``git worktree add`` was invisible to it — measured on one deployment as 68 raw worktrees
unseen, 10 of them holding 136 uncommitted files, while the same command reported the ledger's
107 and nothing else. The reaper had always KEPT those; only its own keep-reason recorded that
they existed, and the surface an operator reads for stranded work reported clean.

These pin both directions plus the two properties that keep the signal usable: a clean orphan
stays absent, and no emitted orphan can ever serialise as the shape the judgment skill routes
to DELETE.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import teatree.core.management.commands._workspace.salvage as ws_salvage_mod
from teatree.core.cleanup.cleanup_emit import CleanupEmitRecord
from teatree.core.management.commands._workspace.salvage import emit_records_json
from teatree.core.worktree import branch_classification
from teatree.core.worktree.orphan_emit import collect_orphan_emit_records
from teatree.utils import git as git_mod
from teatree.utils.run import CommandFailedError
from tests.teatree_core.cleanup._shared import _run_git, corrupt_index
from tests.teatree_core.orphan_fixture import OrphanWorktreeFixture

_real_run_strict = git_mod.run_strict


def _fail_the_two_ref_log_diff_probe(*, repo: str, args: list[str]) -> str:
    """Delegates to the real ``run_strict`` for every call except the log/diff pair.

    Patching ``run_strict`` replaces the attribute on the shared ``teatree.utils.git``
    module object, so an unfiltered ``side_effect`` would also break discovery's own
    ``git worktree list`` call. The two-ref form (``target..ref``) is unique to
    :func:`~teatree.core.worktree.orphan_emit._build_record`'s content probe.
    """
    if args[:1] and args[0] in {"log", "diff"} and len(args) > 1 and ".." in args[1]:
        raise CommandFailedError(["git", *args], 128, "", "boom")
    return _real_run_strict(repo=repo, args=args)


class _OrphanEmitFixture(OrphanWorktreeFixture):
    def _collect(self, *, clean_ignored: bool = False) -> list[CleanupEmitRecord]:
        with (
            patch("teatree.core.worktree.clone_paths.Path.cwd", return_value=self.repo_main),
            patch("teatree.core.worktree.orphan_emit.is_clean_ignored", return_value=clean_ignored),
        ):
            return collect_orphan_emit_records(self.workspace)

    def _record_for(self, wt_path: Path, **kwargs: bool) -> CleanupEmitRecord | None:
        matches = [record for record in self._collect(**kwargs) if record.path == str(wt_path)]
        return matches[0] if matches else None


class TestUncommittedWorkInAnUnregisteredWorktree(_OrphanEmitFixture):
    def test_a_dirty_orphan_is_emitted_with_the_files_it_holds(self) -> None:
        wt_path = self._add_orphan("agent-4579-dirty")
        (wt_path / "wip.py").write_text("GATE = True\n", encoding="utf-8")

        record = self._record_for(wt_path)

        assert record is not None, "a dirty unregistered worktree must reach the stranded-work handoff"
        assert record.uncommitted_paths == ["wip.py"]
        assert record.kind == "orphan-worktree"
        assert record.branch == "agent-4579-dirty"

    def test_a_clean_synced_orphan_is_absent(self) -> None:
        """The control for the case above — without it, an always-emit collector would pass too."""
        wt_path = self._add_orphan("agent-4579-synced", files={"f.txt": "hi"})
        _run_git("push", "-q", "-u", "origin", "agent-4579-synced", cwd=wt_path)

        assert self._record_for(wt_path) is None

    def test_an_unpushed_orphan_is_emitted_with_its_unique_commits(self) -> None:
        wt_path = self._add_orphan("agent-4579-unpushed", files={"new.py": "WORK = 1\n"})

        record = self._record_for(wt_path)

        assert record is not None, "commits on no remote are the other half of what would be lost"
        assert record.unique_commit_shas


class TestTheSignalStaysRoutable(_OrphanEmitFixture):
    def test_an_emitted_orphan_never_serialises_as_the_deletable_shape(self) -> None:
        """``content_verified`` + no unique commits is what the skill routes to DELETE."""
        dirty = self._add_orphan("agent-4579-routable")
        (dirty / "wip.py").write_text("GATE = True\n", encoding="utf-8")
        self._add_orphan("agent-4579-routable-commits", files={"new.py": "WORK = 1\n"})
        detached_squashed = self._add_orphan("agent-4579-routable-detached", files={"sq.py": "X = 1\n"}, detach=True)
        (self.repo_main / "sq.py").write_text("X = 1\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.repo_main)
        _run_git("commit", "-q", "-m", "squash: land sq.py (#1)", cwd=self.repo_main)
        _run_git("push", "-q", "origin", "main", cwd=self.repo_main)

        emitted = [record.to_dict() for record in self._collect()]

        assert emitted, "fixture invalid — nothing was emitted to inspect"
        assert all(not record["content_verified"] or record["unique_commit_shas"] for record in emitted)
        assert not any(record["path"] == str(detached_squashed) for record in emitted), (
            "a detached HEAD whose content is already captured on origin/main holds no "
            "unique work and must not reach the handoff at all"
        )

    def test_a_squash_merged_detached_orphan_is_absent_like_a_synced_named_branch(self) -> None:
        """#4579 hold: orphan_has_unique_work must run the squash check for a detached HEAD too.

        Before the fix, a detached HEAD whose commits are absent-from-all-remotes by SHA but
        whose CONTENT is captured on ``origin/main`` (the ordinary squash-merge shape) was
        treated as unique work by ``orphan_has_unique_work`` (the squash check was skipped for
        ``DETACHED_HEAD``) while ``_build_record`` correctly found it redundant — so the record
        serialised ``content_verified: true`` with empty ``unique_commit_shas``, the exact
        DELETE leaf, while the reaper kept the same checkout as "unpushed work not on any
        remote". Both must now agree it holds nothing worth reporting.
        """
        wt_path = self._add_orphan("agent-4579-detached-squash", files={"new.py": "WORK = 1\n"}, detach=True)
        (self.repo_main / "new.py").write_text("WORK = 1\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.repo_main)
        _run_git("commit", "-q", "-m", "squash: land new.py (#1)", cwd=self.repo_main)
        _run_git("push", "-q", "origin", "main", cwd=self.repo_main)

        assert self._record_for(wt_path) is None, (
            "content already captured on origin/main — must not be reported as unique work"
        )
        reaped = self._reap(dry_run=True)
        assert any(line.startswith("WOULD Reap orphan worktree") and str(wt_path) in line for line in reaped), reaped

    def test_a_clean_ignored_orphan_is_withheld_like_a_ledger_row(self) -> None:
        wt_path = self._add_orphan("agent-4579-ignored")
        (wt_path / "wip.py").write_text("GATE = True\n", encoding="utf-8")

        assert self._record_for(wt_path, clean_ignored=True) is None

    def test_an_unprobeable_orphan_is_emitted_unverified_rather_than_dropped(self) -> None:
        """Silent absence is the failure being fixed, so a probe that cannot answer still reports."""
        wt_path = self._add_orphan("agent-4579-corrupt")
        corrupt_index(wt_path)

        record = self._record_for(wt_path)

        assert record is not None, "an unreadable orphan must not vanish from the handoff"
        assert record.to_dict()["content_verified"] is False


class TestReaperParity(_OrphanEmitFixture):
    def test_emit_names_exactly_the_orphans_the_reaper_keeps(self) -> None:
        """Shared discovery + commit test: emit cannot name a population the reaper reaps."""
        dirty = self._add_orphan("agent-4579-parity-dirty")
        (dirty / "wip.py").write_text("GATE = True\n", encoding="utf-8")
        unpushed = self._add_orphan("agent-4579-parity-unpushed", files={"new.py": "WORK = 1\n"})
        synced = self._add_orphan("agent-4579-parity-synced", files={"f.txt": "hi"})
        _run_git("push", "-q", "-u", "origin", "agent-4579-parity-synced", cwd=synced)

        emitted = {record.path for record in self._collect()}
        reaped = self._reap(dry_run=True)
        kept = {
            str(path)
            for path in (dirty, unpushed, synced)
            if any(line.startswith("KEPT orphan") and str(path) in line for line in reaped)
        }

        assert emitted == kept == {str(dirty), str(unpushed)}


class TestUnreadableCloneDuringCollection(_OrphanEmitFixture):
    @pytest.fixture(autouse=True)
    def _capture_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        self.caplog = caplog

    def test_every_clone_unreadable_returns_empty_and_logs_the_gap(self) -> None:
        """No candidate clone resolves — the empty-orphans early return, with the gap logged."""
        unreadable = self.workspace / "not-a-clone"
        unreadable.mkdir()

        with (
            patch("teatree.core.cleanup.orphan_checkouts.candidate_clones", return_value={str(unreadable)}),
            self.caplog.at_level("WARNING"),
        ):
            records = collect_orphan_emit_records(self.workspace)

        assert records == []
        assert any("could not enumerate a clone" in message for message in self.caplog.messages)


class TestUnreadableContentProbeReportsUnscanned(_OrphanEmitFixture):
    def test_a_failing_log_diff_probe_still_emits_the_record_as_unscanned(self) -> None:
        """The record is built either way — an unreadable diff is ``unknown``, never a crash."""
        wt_path = self._add_orphan("agent-4579-unscanned")
        (wt_path / "wip.py").write_text("GATE = True\n", encoding="utf-8")

        with (
            patch("teatree.core.worktree.orphan_emit.git.run_strict", side_effect=_fail_the_two_ref_log_diff_probe),
            patch("teatree.core.worktree.clone_paths.Path.cwd", return_value=self.repo_main),
            patch("teatree.core.worktree.orphan_emit.is_clean_ignored", return_value=False),
        ):
            records = collect_orphan_emit_records(self.workspace)

        matches = [record for record in records if record.path == str(wt_path)]
        assert matches, "a failing content probe must not drop the record"
        record = matches[0]
        assert record.banned_terms_status == "unknown"
        assert record.banned_terms_found == []


class TestAnUnresolvableDefaultBranchNeverSilencesTheHandoff(_OrphanEmitFixture):
    def test_one_clone_with_no_resolvable_default_still_renders_the_whole_emit(self) -> None:
        """The union must lose neither side; aborting the command loses BOTH.

        ``git.default_branch`` raises ``RuntimeError`` on a clone with no ``origin/HEAD`` and
        no ``origin/{main,master,development}``, and nothing between the orphan probe and the
        CLI catches it — so ``workspace emit`` printed nothing at all, including the ledger
        records that always rendered before.
        """
        wt_path = self._add_orphan("agent-4579-no-default", files={"new.py": "WORK = 1\n"})
        _run_git("update-ref", "-d", "refs/remotes/origin/main", cwd=self.repo_main)
        ledger = CleanupEmitRecord(path="/ws/feat", branch="feat", kind="worktree")

        with (
            patch("teatree.core.worktree.clone_paths.Path.cwd", return_value=self.repo_main),
            patch("teatree.core.worktree.orphan_emit.is_clean_ignored", return_value=False),
            patch.object(ws_salvage_mod, "collect_emit_records", return_value=[ledger]),
        ):
            rendered = json.loads(emit_records_json(self.workspace))

        assert [row["path"] for row in rendered] == ["/ws/feat", str(wt_path)]
        assert rendered[1]["content_verified"] is False, "an unprobeable base can never authorise a DELETE"


class TestTheTwoDefaultTargetsCannotDiverge(_OrphanEmitFixture):
    """A pinned (``single_branch_repos``) repo made the two probes measure against DIFFERENT bases.

    ``orphan_has_unique_work`` resolved ``origin/<git.default_branch>`` while ``_build_record``
    honoured the pin — so a checkout squash-landed on the PINNED branch read as work-bearing
    (emitted) and simultaneously as redundant (``content_verified: true``, no unique commits):
    exactly the DELETE leaf the sibling anti-vacuity control asserts is unreachable.
    """

    def _pin_the_repo_to(self, branch: str) -> None:
        _run_git("branch", branch, "main", cwd=self.repo_main)
        _run_git("push", "-q", "-u", "origin", branch, cwd=self.repo_main)

    def _land_on(self, branch: str, name: str, content: str) -> None:
        _run_git("checkout", "-q", branch, cwd=self.repo_main)
        (self.repo_main / name).write_text(content, encoding="utf-8")
        _run_git("add", "-A", cwd=self.repo_main)
        _run_git("commit", "-q", "-m", f"squash: land {name} (#1)", cwd=self.repo_main)
        _run_git("push", "-q", "origin", branch, cwd=self.repo_main)
        _run_git("checkout", "-q", "main", cwd=self.repo_main)

    def test_work_landed_on_the_pinned_branch_is_absent_not_a_deletable_record(self) -> None:
        self._pin_the_repo_to("bootstrap")
        wt_path = self._add_orphan("agent-4579-pinned", files={"new.py": "WORK = 1\n"})
        self._land_on("bootstrap", "new.py", "WORK = 1\n")

        with patch.object(branch_classification, "_declared_single_branch_repos", return_value=("origin=bootstrap",)):
            record = self._record_for(wt_path)

        assert record is None, "both probes must measure against the pinned branch the work landed on"


class TestTheForgeMemoIsResetLikeTheSiblingPasses(_OrphanEmitFixture):
    def test_a_previous_ticks_merged_answer_is_not_reused(self) -> None:
        """The memo is process-wide, so a long-lived loop worker carries it between ticks."""
        repo, branch = str(self.repo_main), "agent-4579-memo"
        wt_path = self._add_orphan(branch, files={"new.py": "WORK = 1\n"})
        with patch.object(branch_classification, "probe_host_cli", return_value="7"):
            assert branch_classification._branch_pr_is_merged(repo, branch) is True

        with patch.object(branch_classification, "probe_host_cli", return_value=""):
            assert self._record_for(wt_path) is not None
            assert branch_classification._branch_pr_is_merged(repo, branch) is False


class TestDetachedOrphansWithUnsquashedContentStayKept(_OrphanEmitFixture):
    def test_a_detached_orphan_whose_content_landed_nowhere_is_emitted_and_kept(self) -> None:
        """The negative control for the newly-authorised detached reap (#4579's squash arm).

        The positive case — a detached HEAD whose content IS captured on ``origin/main`` —
        is pinned above. Without this, running the squash check for a detached HEAD could
        widen into reaping every detached orphan and both surfaces would still read green.
        """
        wt_path = self._add_orphan("agent-4579-detached-unique", files={"only-here.py": "WORK = 1\n"}, detach=True)

        record = self._record_for(wt_path)

        assert record is not None, "content on no remote and on no base must reach the handoff"
        assert record.unique_commit_shas, "the commit the reaper refuses to destroy must be named"
        reaped = self._reap(dry_run=True)
        assert any(line.startswith("KEPT orphan") and str(wt_path) in line for line in reaped), reaped
