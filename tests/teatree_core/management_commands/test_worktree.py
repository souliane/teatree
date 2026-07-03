"""``t3 <overlay> worktree status`` renders the last provision report."""

import pytest
from django.test import TestCase

from teatree.core.management.commands.worktree import _provision_summary
from teatree.core.models import Ticket, Worktree


def _step(name: str, *, success: bool = True, duration: float = 0.0, error: str = "") -> dict[str, object]:
    return {"name": name, "success": success, "duration": duration, "error": error, "required": True, "skipped": False}


class TestProvisionSummary(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/1")

    def _worktree(self, extra: dict[str, object]) -> Worktree:
        return Worktree.objects.create(ticket=self.ticket, repo_path="backend", branch="b", extra=extra)

    def test_none_when_never_provisioned(self) -> None:
        worktree = self._worktree({})
        assert _provision_summary(worktree) is None

    def test_renders_total_duration_and_success(self) -> None:
        worktree = self._worktree(
            {
                "provision_report": {
                    "success": True,
                    "total_duration": 12.5,
                    "steps": [_step("a", duration=2.0), _step("b", duration=10.5)],
                }
            }
        )
        summary = _provision_summary(worktree)
        assert summary is not None
        assert summary["success"] is True
        assert summary["steps"] == 2
        assert summary["total_duration"] == pytest.approx(12.5)
        assert summary["slowest_step"] == "b"
        assert summary["slowest_step_duration"] == pytest.approx(10.5)

    def test_renders_failure(self) -> None:
        worktree = self._worktree(
            {
                "provision_report": {
                    "success": False,
                    "total_duration": 1.0,
                    "steps": [_step("a", success=False, duration=1.0, error="x")],
                }
            }
        )
        summary = _provision_summary(worktree)
        assert summary is not None
        assert summary["success"] is False

    def test_none_when_extra_is_none(self) -> None:
        worktree = Worktree.objects.create(ticket=self.ticket, repo_path="backend", branch="b")
        worktree.extra = None
        assert _provision_summary(worktree) is None
