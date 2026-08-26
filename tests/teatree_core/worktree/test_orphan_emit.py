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

from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.core.cleanup.cleanup_emit import CleanupEmitRecord
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

        emitted = [record.to_dict() for record in self._collect()]

        assert emitted, "fixture invalid — nothing was emitted to inspect"
        assert all(not record["content_verified"] or record["unique_commit_shas"] for record in emitted)

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
