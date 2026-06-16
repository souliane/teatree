"""Tests for ``t3 info artifacts <ticket>`` (#273).

The command renders a ticket's read-only artifact aggregation — worktree
on-disk path + ports + db_name + state, PlanArtifact rows, Task
``result_artifact_path`` values, and E2eMandatoryRun evidence (spec + posted
video/comment URL) — in both the terse text view and a parseable ``--format
json`` payload. The live host-port resolver is patched so the command never
shells out to docker.
"""

import json
from io import StringIO

import pytest
from django.core.management import call_command

from teatree.core.models import E2eMandatoryRun, PlanArtifact, Session, Task, Ticket, Worktree

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _call(*args: str) -> str:
    buf = StringIO()
    call_command(*args, stdout=buf)
    return buf.getvalue()


def _populated_ticket() -> Ticket:
    ticket = Ticket.objects.create(
        overlay="t3-teatree",
        issue_url="https://github.com/example/repo/issues/273",
    )
    Worktree.objects.create(
        ticket=ticket,
        repo_path="example/repo",
        branch="ac/273",
        db_name="wt_273",
        state=Worktree.State.READY,
        extra={"worktree_path": "/ws/273/example-repo"},
    )
    PlanArtifact.record(ticket=ticket, plan_text="the plan", recorded_by="planner")
    session = Session.objects.create(ticket=ticket, agent_id="coding")
    Task.objects.create(ticket=ticket, session=session, phase="coding", result_artifact_path="/runs/a.jsonl")
    E2eMandatoryRun.record(
        ticket=ticket,
        head_sha="a" * 40,
        spec="e2e/login.spec.ts",
        result=E2eMandatoryRun.Result.GREEN,
        posted_url="https://github.com/example/repo/issues/273#note-1",
    )
    return ticket


@pytest.fixture(autouse=True)
def _stub_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    """No live docker query — the command resolves a fixed port map."""
    monkeypatch.setattr(
        "teatree.core.management.commands.info.get_worktree_ports",
        lambda *_a, **_k: {"backend": 18000, "frontend": 18080},
    )


class TestInfoArtifactsText:
    def test_text_lists_every_source(self) -> None:
        ticket = _populated_ticket()

        out = _call("info", "artifacts", str(ticket.pk))

        assert "/ws/273/example-repo" in out
        assert "wt_273" in out
        assert "ready" in out
        assert "backend=18000" in out
        assert "/runs/a.jsonl" in out
        assert "e2e/login.spec.ts" in out
        assert "https://github.com/example/repo/issues/273#note-1" in out
        assert "the plan" in out or "planner" in out

    def test_empty_ticket_is_a_clean_report(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/example/repo/issues/9")

        out = _call("info", "artifacts", str(ticket.pk))

        assert str(ticket.pk) in out
        # No traceback, no crash on an artifact-free ticket.
        assert "Traceback" not in out


class TestInfoArtifactsJson:
    def test_json_is_parseable_and_carries_every_source(self) -> None:
        ticket = _populated_ticket()

        payload = json.loads(_call("info", "artifacts", str(ticket.pk), "--format", "json"))

        assert payload["ticket_id"] == ticket.pk
        assert payload["worktrees"][0]["worktree_path"] == "/ws/273/example-repo"
        assert payload["worktrees"][0]["db_name"] == "wt_273"
        assert payload["worktrees"][0]["state"] == "ready"
        assert payload["worktrees"][0]["ports"] == {"backend": 18000, "frontend": 18080}
        assert payload["result_artifact_paths"] == ["/runs/a.jsonl"]
        assert payload["e2e_runs"][0]["spec"] == "e2e/login.spec.ts"
        assert payload["e2e_runs"][0]["posted_url"] == "https://github.com/example/repo/issues/273#note-1"
        assert payload["plan_artifacts"][0]["recorded_by"] == "planner"

    def test_json_empty_ticket_has_empty_collections(self) -> None:
        ticket = Ticket.objects.create()

        payload = json.loads(_call("info", "artifacts", str(ticket.pk), "--format", "json"))

        assert payload["ticket_id"] == ticket.pk
        assert payload["worktrees"] == []
        assert payload["plan_artifacts"] == []
        assert payload["result_artifact_paths"] == []
        assert payload["e2e_runs"] == []


class TestInfoArtifactsRefusals:
    def test_unknown_ticket_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            _call("info", "artifacts", "999999")

    def test_unknown_format_exits_two(self) -> None:
        ticket = Ticket.objects.create()
        with pytest.raises(SystemExit) as exc:
            _call("info", "artifacts", str(ticket.pk), "--format", "yaml")
        assert exc.value.code == 2
