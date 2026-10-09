"""The squash-merge reconcile never hard-resets without a proven recovery ref.

``git reset --hard`` is authorized here by a content-equivalence gate PLUS the
``refs/t3-reconcile-backup/<sha>`` net that makes a misclassification recoverable.
``git update-ref``'s exit code was discarded, so a failed ref write still reset the
clone and the success line named a ref that does not exist.
"""

from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from teatree.cli._update_reconcile import reconcile_squash_merged
from teatree.cli.update import UpdateStatus
from teatree.core.worktree.branch_classification import SubjectPrefilterResult


def _ok(stdout: str = "") -> CompletedProcess[str]:
    return CompletedProcess(args=["git"], returncode=0, stdout=stdout, stderr="")


def _failed(stderr: str) -> CompletedProcess[str]:
    return CompletedProcess(args=["git"], returncode=1, stdout="", stderr=stderr)


class _GitStub:
    """Answers the reconcile's git calls; ``update_ref_rc`` drives the backup-ref write."""

    def __init__(self, *, update_ref_rc: int) -> None:
        self.update_ref_rc = update_ref_rc
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, _repo: Path, *args: str, **_kw: object) -> CompletedProcess[str]:
        self.commands.append(args)
        if args[0] == "update-ref":
            return _ok() if self.update_ref_rc == 0 else _failed("cannot lock ref")
        if args[0] == "rev-parse":
            return _ok("a" * 40 if args[1] == "HEAD" else "bbbbbbb")
        if args[0] == "rev-list":
            return _ok("1")
        if args[0] == "symbolic-ref":
            return _ok("main")
        return _ok()

    @property
    def reset_ran(self) -> bool:
        return any(cmd[:2] == ("reset", "--hard") for cmd in self.commands)


@pytest.fixture
def reconcile_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "teatree.cli._update_reconcile.prefilter_branch_commits_by_subject",
        lambda *_a, **_kw: SubjectPrefilterResult(),
    )
    monkeypatch.setattr("teatree.cli._update_reconcile.content_equivalence_blockers", lambda *_a, **_kw: [])


@pytest.mark.usefixtures("reconcile_env")
class TestTheResetRequiresARecoveryRef:
    def test_a_failed_backup_ref_refuses_the_reset(self, tmp_path: Path) -> None:
        stub = _GitStub(update_ref_rc=1)
        with patch("teatree.cli.update._git", stub):
            outcome = reconcile_squash_merged("teatree", tmp_path, "old", "not possible to fast-forward")

        assert outcome.status is UpdateStatus.FAILED
        assert "recovery ref" in outcome.reason
        assert not stub.reset_ran

    def test_a_written_backup_ref_lets_the_reset_proceed(self, tmp_path: Path) -> None:
        stub = _GitStub(update_ref_rc=0)
        with patch("teatree.cli.update._git", stub):
            outcome = reconcile_squash_merged("teatree", tmp_path, "old", "not possible to fast-forward")

        assert stub.reset_ran
        assert outcome.status is UpdateStatus.UPDATED
