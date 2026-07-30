"""Tests for WorktreeVerifyRunner."""

from dataclasses import dataclass
from unittest.mock import MagicMock

from teatree.core.runners.worktree_verify import WorktreeVerifyRunner


@dataclass
class _Check:
    name: str
    description: str = ""
    passes: bool = True

    def check(self) -> bool:
        return self.passes


def test_all_checks_pass() -> None:
    overlay = MagicMock()
    overlay.provisioning.health_checks.return_value = [_Check("db"), _Check("redis")]
    runner = WorktreeVerifyRunner(worktree=MagicMock(), overlay=overlay)
    result = runner.run()
    assert result.ok
    assert "2 check(s) ok" in result.detail


def test_failing_check_reported() -> None:
    overlay = MagicMock()
    overlay.provisioning.health_checks.return_value = [_Check("db", passes=False)]
    runner = WorktreeVerifyRunner(worktree=MagicMock(), overlay=overlay)
    result = runner.run()
    assert not result.ok
    assert "db" in result.detail


def test_exception_in_check_caught() -> None:
    bad_check = MagicMock()
    bad_check.name = "boom"
    bad_check.check.side_effect = RuntimeError("exploded")
    overlay = MagicMock()
    overlay.provisioning.health_checks.return_value = [bad_check]
    runner = WorktreeVerifyRunner(worktree=MagicMock(), overlay=overlay)
    result = runner.run()
    assert not result.ok
    assert "boom" in result.detail
