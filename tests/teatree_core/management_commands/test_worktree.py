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


class TestWorktreeHumanRenderers:
    """The non-JSON human view of ``worktree status``/``diagnose`` (PR-30).

    Routed to stderr by the emit seam so stdout stays a pure JSON channel under
    ``--json``.
    """

    def test_render_status_writes_state_branch_ports_and_provision(self) -> None:
        from io import StringIO  # noqa: PLC0415

        from teatree.core.management.commands.worktree import WorktreeStatus, _render_status  # noqa: PLC0415

        status: WorktreeStatus = {
            "state": "ready",
            "repo_path": "backend",
            "branch": "feat/x",
            "ports": {"backend": 8010},
            "provision_report": {
                "total_duration": 12.5,
                "steps": 3,
                "success": True,
                "slowest_step": "db-import",
                "slowest_step_duration": 10.0,
            },
        }
        buf = StringIO()
        _render_status(status, buf)
        out = buf.getvalue()
        assert "state: ready" in out
        assert "branch: feat/x" in out
        assert "db-import" in out

    def test_render_diagnose_writes_checklist(self) -> None:
        from io import StringIO  # noqa: PLC0415

        from teatree.core.management.commands.worktree import WorktreeDiagnose, _render_diagnose  # noqa: PLC0415

        checks: WorktreeDiagnose = {
            "state": "provisioned",
            "repo_path": "backend",
            "worktree_dir": True,
            "git_marker": False,
            "env_cache": True,
            "db_name": "wt_db",
            "docker_services": "not running",
        }
        buf = StringIO()
        _render_diagnose(checks, buf)
        out = buf.getvalue()
        assert "backend (provisioned)" in out
        assert "[OK] worktree_dir" in out
        assert "[FAIL] git_marker" in out
        assert "DB name: wt_db" in out
