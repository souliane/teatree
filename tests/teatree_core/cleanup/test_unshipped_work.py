"""Capture of a checkout's unshipped work — real git under ``tmp_path``.

The population these guard is the agent scratch checkout: staged-but-uncommitted
edits, tracked modifications, and commits on no remote. The load-bearing case is
the STAGED-ONLY one — ``git diff`` compares the working tree to the INDEX, so a
checkout whose entire delta is staged reads as 0 bytes of diff while holding real
work. Every assertion here is driven through the production capture, so a probe
that regressed to a bare ``git diff`` reports an empty patch and fails.
"""

import itertools
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from django.db import OperationalError
from django.test import TestCase

from teatree.core.cleanup.unshipped_work import bundle_path, capture_unshipped_work, probe_unshipped_work
from teatree.core.models import UnshippedWorkRecord
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git


def _git_out(*args: str, cwd: Path) -> str:
    return subprocess.run(
        [_GIT, "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=_clean_env()
    ).stdout


class _CheckoutFixture(TestCase):
    """A clone pushed to a bare origin, plus a worktree the tests dirty."""

    @pytest.fixture(autouse=True)
    def _tmp_checkout(self, tmp_path: Path) -> None:
        self.artifacts = tmp_path / "artifacts"
        self.origin = tmp_path / "origin.git"
        self.origin.mkdir()
        _run_git("init", "-q", "--bare", "-b", "main", cwd=self.origin)
        self.clone = tmp_path / "clone"
        self.clone.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.clone)
        _run_git("config", "user.email", "t@t", cwd=self.clone)
        _run_git("config", "user.name", "t", cwd=self.clone)
        _run_git("remote", "add", "origin", str(self.origin), cwd=self.clone)
        (self.clone / "tracked.py").write_text("value = 1\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.clone)
        _run_git("commit", "-q", "-m", "initial", cwd=self.clone)
        _run_git("push", "-q", "-u", "origin", "main", cwd=self.clone)
        self.checkout = tmp_path / "agent-deadbeef"
        _run_git("worktree", "add", "-q", "-b", "feat", str(self.checkout), cwd=self.clone)

    def _stage_only(self) -> None:
        """Stage a tracked modification and leave the working tree matching the index."""
        (self.checkout / "tracked.py").write_text("value = 2\n", encoding="utf-8")
        _run_git("add", "tracked.py", cwd=self.checkout)

    def _commit_locally(self, subject: str) -> str:
        (self.checkout / f"{subject}.py").write_text("x = 1\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.checkout)
        _run_git("commit", "-q", "-m", subject, cwd=self.checkout)
        return _git_out("rev-parse", "HEAD", cwd=self.checkout).strip()


class TestProbeSeesTheIndex(_CheckoutFixture):
    def test_staged_only_checkout_is_dirty_with_the_staged_hunk_in_the_patch(self) -> None:
        self._stage_only()
        assert _git_out("diff", cwd=self.checkout) == "", "fixture invalid: bare `git diff` must be blind here"

        work = probe_unshipped_work(self.checkout)

        assert work.exists
        assert work.dirty_paths == ["tracked.py"]
        assert "value = 2" in work.uncommitted_patch

    def test_unstaged_modification_is_dirty(self) -> None:
        (self.checkout / "tracked.py").write_text("value = 3\n", encoding="utf-8")

        work = probe_unshipped_work(self.checkout)

        assert work.dirty_paths == ["tracked.py"]
        assert "value = 3" in work.uncommitted_patch

    def test_untracked_file_is_recorded_as_dirty(self) -> None:
        (self.checkout / "scratch.md").write_text("notes\n", encoding="utf-8")

        work = probe_unshipped_work(self.checkout)

        assert work.dirty_paths == ["scratch.md"]

    def test_a_path_containing_a_space_is_recorded_verbatim(self) -> None:
        (self.checkout / "notes for later.md").write_text("notes\n", encoding="utf-8")

        work = probe_unshipped_work(self.checkout)

        assert work.dirty_paths == ["notes for later.md"]

    def test_a_rename_records_both_endpoints_not_an_arrow(self) -> None:
        _run_git("mv", "tracked.py", "renamed.py", cwd=self.checkout)

        work = probe_unshipped_work(self.checkout)

        assert work.dirty_paths == ["renamed.py", "tracked.py"]

    def test_unpushed_commits_are_captured_oldest_first(self) -> None:
        self._commit_locally("first")
        self._commit_locally("second")

        work = probe_unshipped_work(self.checkout)

        assert [line.split(maxsplit=1)[1] for line in work.unpushed_commits] == ["second", "first"]
        assert work.commits_patch.index("first") < work.commits_patch.index("second")

    def test_clean_synced_checkout_holds_nothing(self) -> None:
        work = probe_unshipped_work(self.checkout)

        assert not work.exists
        assert work.dirty_paths == []
        assert work.unpushed_commits == []

    def test_present_but_unreadable_checkout_counts_as_holding_work(self) -> None:
        not_a_repo = self.checkout.parent / "agent-notgit"
        not_a_repo.mkdir()

        work = probe_unshipped_work(not_a_repo)

        assert work.exists
        assert work.unreadable

    def test_absent_checkout_holds_nothing(self) -> None:
        work = probe_unshipped_work(self.checkout.parent / "does-not-exist")

        assert not work.exists


class TestWrongVenueIsNamedAsSuch(_CheckoutFixture):
    """A checkout whose admin dir belongs to another execution context (#4272).

    ``t3`` runs in Docker, so a container-created checkout records a gitdir the
    host cannot resolve, and git answers in the same words it uses for a directory
    that never held a repository. Reported as a repository verdict, the operator
    reads a broken worktree and hand-probes it; reported as a venue, they know to
    probe from the container instead.
    """

    def _misdirect_gitdir(self) -> Path:
        elsewhere = Path("/nonexistent-venue/clone/.git/worktrees/agent-deadbeef")
        (self.checkout / ".git").write_text(f"gitdir: {elsewhere}\n", encoding="utf-8")
        return elsewhere

    def test_unresolvable_gitdir_reports_the_venue_not_a_repository_verdict(self) -> None:
        elsewhere = self._misdirect_gitdir()

        work = probe_unshipped_work(self.checkout)

        assert work.exists, "a checkout this venue cannot read has not been proven empty"
        assert str(elsewhere) in work.unreadable
        assert "does not exist in this execution context" in work.unreadable
        assert "HEAD" not in work.unreadable, f"a venue miss is not a repository verdict: {work.unreadable}"

    def test_a_genuine_non_checkout_keeps_the_repository_verdict(self) -> None:
        not_a_repo = self.checkout.parent / "agent-notgit"
        not_a_repo.mkdir()

        work = probe_unshipped_work(not_a_repo)

        assert "execution context" not in work.unreadable, (
            "a dir that never claimed to be a checkout must not be excused as a venue miss"
        )


class TestCaptureWritesArtifactsAndRow(_CheckoutFixture):
    def _capture(self) -> UnshippedWorkRecord | None:
        return capture_unshipped_work(self.checkout, branch="feat", overlay="test", artifact_root=self.artifacts)

    def test_staged_only_capture_writes_the_four_artifacts(self) -> None:
        self._stage_only()

        record = self._capture()

        assert record is not None
        prefix = record.artifact_prefix
        assert "value = 2" in bundle_path(prefix, ".uncommitted.patch").read_text(encoding="utf-8")
        assert bundle_path(prefix, ".files").read_text(encoding="utf-8").split() == ["tracked.py"]
        assert bundle_path(prefix, ".commits.patch").read_text(encoding="utf-8") == ""
        meta = bundle_path(prefix, ".meta").read_text(encoding="utf-8")
        assert f"worktree={self.checkout}" in meta
        assert "branch=feat" in meta
        assert "dirty=1 ahead=0" in meta

    def test_capture_records_a_durable_row(self) -> None:
        sha = self._commit_locally("later")
        self._stage_only()

        record = self._capture()

        assert record is not None
        assert record.checkout_path == str(self.checkout)
        assert record.branch == "feat"
        assert record.overlay == "test"
        assert record.dirty_paths == ["tracked.py"]
        assert [line.split()[0] for line in record.unpushed_commits] == [sha[:7]]

    def test_recapture_updates_the_same_row(self) -> None:
        self._stage_only()
        self._capture()
        (self.checkout / "second.py").write_text("y = 1\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.checkout)

        record = self._capture()

        assert record is not None
        assert UnshippedWorkRecord.objects.count() == 1
        assert record.dirty_paths == ["second.py", "tracked.py"]

    def test_bundle_stem_survives_a_dot_in_the_checkout_name(self) -> None:
        dotted = self.checkout.parent / "agent-fix-v1.2"
        _run_git("worktree", "add", "-q", "-b", "dotted", str(dotted), cwd=self.clone)
        (dotted / "wip.txt").write_text("edit", encoding="utf-8")

        record = capture_unshipped_work(dotted, branch="dotted", artifact_root=self.artifacts)

        assert record is not None
        assert bundle_path(record.artifact_prefix, ".files").read_text(encoding="utf-8") == "wip.txt\n"

    def test_same_named_checkouts_do_not_share_a_bundle(self) -> None:
        self._stage_only()
        twin = self.checkout.parent / "twin" / self.checkout.name
        _run_git("worktree", "add", "-q", "-b", "twin-feat", str(twin), cwd=self.clone)
        (twin / "other.txt").write_text("other", encoding="utf-8")

        first = self._capture()
        second = capture_unshipped_work(twin, branch="twin-feat", artifact_root=self.artifacts)

        assert first is not None
        assert second is not None
        assert first.artifact_prefix != second.artifact_prefix

    def test_clean_checkout_captures_nothing(self) -> None:
        assert self._capture() is None
        assert not UnshippedWorkRecord.objects.exists()
        assert not self.artifacts.exists()


class TestCaptureNeverRaises(_CheckoutFixture):
    """The capture runs ahead of every teardown guard, so it must degrade, never raise.

    A control DB that is locked, unmigrated, or simply unreachable is a routine
    state on a box running concurrent agents — and this call sits ahead of the
    reaping decision, so an exception here wedges the teardown itself.
    """

    _LOGGER = "teatree.core.cleanup.unshipped_work"

    def _capture(self) -> UnshippedWorkRecord | None:
        return capture_unshipped_work(self.checkout, branch="feat", overlay="test", artifact_root=self.artifacts)

    def test_a_transient_database_lock_is_retried_and_the_row_still_lands(self) -> None:
        self._stage_only()
        real = UnshippedWorkRecord.objects.update_or_create
        attempts = itertools.count()
        locked = OperationalError("database is locked")

        def locked_once(**kwargs: Any) -> tuple[UnshippedWorkRecord, bool]:
            if next(attempts) == 0:
                raise locked
            return real(**kwargs)

        with patch.object(UnshippedWorkRecord.objects, "update_or_create", side_effect=locked_once):
            record = self._capture()

        assert record is not None, "a transient lock must be retried, not dropped"
        assert record.dirty_paths == ["tracked.py"]

    def test_an_unwritable_control_db_degrades_to_a_logged_none(self) -> None:
        self._stage_only()

        with (
            patch.object(
                UnshippedWorkRecord.objects,
                "update_or_create",
                side_effect=OperationalError("no such table: teatree_unshippedworkrecord"),
            ),
            self.assertLogs(self._LOGGER, level="WARNING") as logs,
        ):
            assert self._capture() is None

        assert any(str(self.checkout) in line for line in logs.output), logs.output

    def test_an_unreachable_artifact_root_degrades_to_a_logged_none(self) -> None:
        self._stage_only()

        with (
            patch(
                "teatree.core.cleanup.unshipped_work.get_data_dir",
                side_effect=OSError("read-only file system"),
            ),
            self.assertLogs(self._LOGGER, level="WARNING") as logs,
        ):
            assert capture_unshipped_work(self.checkout, branch="feat") is None

        assert any(str(self.checkout) in line for line in logs.output), logs.output

    def test_an_unwritable_bundle_still_leaves_the_durable_row(self) -> None:
        self._stage_only()

        with patch(
            "teatree.core.cleanup.unshipped_work.Path.write_text",
            side_effect=OSError("no space left on device"),
        ):
            record = self._capture()

        assert record is not None, "the row is the durable half — a failed bundle must not lose it"
        assert record.dirty_paths == ["tracked.py"]
