"""An author's self-review as a returned reviewing envelope records it: a completed task plus its attempt."""

from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt, Ticket
from tests.factories import planned_ticket
from tests.teatree_core.conftest import record_maker_review_for_test, record_review_context_for_test

HELD_SHA = "5e1f0a9c3b7d2e4f6a8c0b1d3e5f7a9c2b4d6e8f"
LONG_SUMMARY = "the rework reason drops the reviewed head; " + "keep every word of a long finding " * 30 + "FIX-TAIL"
FAILURE_SCENARIO = "A replayed HOLD queues a second rework and the coder fixes the same finding twice."
HELD_FINDINGS: list[dict[str, object]] = [
    {
        "severity": "major",
        "summary": "the HOLD branch never re-reads the ticket",
        "file": "src/teatree/x.py",
        "line": 12,
    },
    {
        "severity": "minor",
        "summary": LONG_SUMMARY,
        "file": "src/teatree/y.py",
        "line": 40,
        "failure_scenario": FAILURE_SCENARIO,
    },
]


def self_review_result(
    verdict: str, *, findings: list[dict[str, object]] | None = None, reviewed_sha: str = HELD_SHA
) -> dict[str, object]:
    return {
        "summary": "Self-review of the ticket branch against its acceptance criteria.",
        "review_verdict": {
            "verdict": verdict,
            "reviewed_sha": reviewed_sha,
            "reviewer_identity": "self-reviewer-agent",
            "findings": HELD_FINDINGS if findings is None and verdict.strip().lower() == "hold" else (findings or []),
        },
        "review_context": {
            "work_item": "https://github.com/souliane/teatree/issues/5076",
            "documents": ["https://example.test/specification/5076"],
            "analysis": "Compared the branch diff with every acceptance criterion of the issue.",
        },
        "anti_vacuity": {
            "ac_coverage": "Every acceptance criterion maps to a test in the branch.",
            "proven_tests": ["tests/teatree_core/test_task_apply_phase_transition.py::test_hold"],
            "no_new_tests": False,
        },
    }


def author_ticket(*, state: str = Ticket.State.TESTED) -> Ticket:
    ticket = planned_ticket(
        overlay="test",
        role=Ticket.Role.AUTHOR,
        state=state,
        issue_url="https://github.com/souliane/teatree/issues/5076",
    )
    record_review_context_for_test(ticket)
    record_maker_review_for_test(ticket, HELD_SHA)
    return ticket


def completed_self_review(
    ticket: Ticket,
    verdict: str,
    *,
    findings: list[dict[str, object]] | None = None,
    reviewed_sha: str = HELD_SHA,
    attempts: int = 1,
) -> Task:
    session = Session.objects.create(ticket=ticket, agent_id="review")
    task = Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.COMPLETED)
    for _ in range(attempts):
        TaskAttempt.objects.create(
            task=task,
            ended_at=timezone.now(),
            exit_code=0,
            result=self_review_result(verdict, findings=findings, reviewed_sha=reviewed_sha),
        )
    return task


def head(index: int) -> str:
    return f"{index:040x}"
