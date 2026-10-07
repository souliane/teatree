"""An author's pre-PR self-review verdict, read back from the reviewing attempt that returned it.

No pull request exists yet, so no ``ReviewVerdict`` row can hold it: ``TaskAttempt.result``
already persists the returned ``review_verdict``, and the newest one of the current delivery
cycle governs.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, cast

from teatree.core.models.errors import SelfReviewReworkRefusedError
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.review_verdict import Finding, ReviewVerdict
from teatree.core.models.ticket import Ticket

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    from teatree.core.managers import TaskQuerySet
    from teatree.core.models.task import Task

_REASON_CAP = 16_000
_ELISION_ROOM = 120


@dataclass(frozen=True, slots=True)
class HeldFinding:
    finding: Finding
    failure_scenario: str = ""

    @classmethod
    def from_dict(cls, raw: "Mapping[str, object]") -> "HeldFinding":
        return cls(Finding.from_dict(raw), str(raw.get("failure_scenario") or "").strip())

    def render(self) -> str:
        line = f"- {self.finding.describe()}"
        return f"{line}\n  failure scenario: {self.failure_scenario}" if self.failure_scenario else line


@dataclass(frozen=True, slots=True)
class SelfReview:
    task_pk: int
    verdict: str
    reviewed_sha: str
    findings: tuple[HeldFinding, ...]

    @classmethod
    def from_result(cls, task_pk: int, result: object) -> "SelfReview | None":
        """The verdict *result* returned, or ``None`` — a run parked for user input concluded nothing."""
        if not isinstance(result, dict) or result.get("needs_user_input") is True:
            return None
        returned = result.get("review_verdict")
        if not isinstance(returned, dict):
            return None
        findings = returned.get("findings")
        return cls(
            task_pk=task_pk,
            verdict=str(returned.get("verdict", "")).strip().lower(),
            reviewed_sha=str(returned.get("reviewed_sha") or "").strip().lower(),
            findings=tuple(HeldFinding.from_dict(row) for row in findings if isinstance(row, dict))
            if isinstance(findings, list)
            else (),
        )

    @classmethod
    def of_task(cls, task: "Task") -> "SelfReview | None":
        return next(cls._newest_first(type(task).objects.filter(pk=task.pk)), None)

    @classmethod
    def latest_for(cls, ticket: Ticket) -> "SelfReview | None":
        return next(cls._current_cycle(ticket), None)

    @classmethod
    def open_hold_for(cls, ticket: Ticket) -> "SelfReview | None":
        """The current cycle's newest self-review when it is a HOLD its rework has not yet cleared."""
        review = cls.latest_for(ticket)
        return review if review is not None and review.is_hold else None

    @classmethod
    def held_reviews(cls, ticket: Ticket) -> set[int]:
        """The reviewing tasks whose newest verdict in this delivery cycle is a HOLD: one per rework lap."""
        newest: dict[int, SelfReview] = {}
        for review in cls._current_cycle(ticket):
            newest.setdefault(review.task_pk, review)
        return {task_pk for task_pk, review in newest.items() if review.is_hold}

    @classmethod
    def held_for_rework(cls, ticket: Ticket) -> "SelfReview":
        """The HOLD a ticket parked past it still owes its rework, else refuse naming why."""
        if ticket.role != Ticket.Role.AUTHOR:
            msg = f"ticket {ticket.pk} is a reviewer ticket; only an author's own self-review holds its branch"
            raise SelfReviewReworkRefusedError(msg, hint="Run rework-hold on the author ticket that owns the branch.")
        if ticket.state not in {Ticket.State.TESTED, Ticket.State.SELF_REVIEWED}:
            msg = f"ticket {ticket.pk} is {ticket.state}; a HOLD is reworked only at tested or self_reviewed"
            raise SelfReviewReworkRefusedError(msg, hint="Let the ticket reach its self-review first.")
        if pr := PullRequest.objects.live().filter(ticket=ticket).values_list("url", flat=True).first():
            msg = f"ticket {ticket.pk} has an open pull request ({pr})"
            raise SelfReviewReworkRefusedError(msg, hint=OPEN_PR_HINT)
        review = cls.open_hold_for(ticket)
        if review is None:
            msg = f"ticket {ticket.pk}'s latest self-review is not a HOLD; there is nothing to rework"
            raise SelfReviewReworkRefusedError(msg, hint="A merge_safe self-review lets the ticket ship as it is.")
        return review

    @classmethod
    def _current_cycle(cls, ticket: Ticket) -> "Iterator[SelfReview]":
        if ticket.role != Ticket.Role.AUTHOR:
            return iter(())
        reviews = ticket.tasks.completed_in_phase("reviewing")
        if (cycle_start := _delivery_cycle_start(ticket)) is not None:
            reviews = reviews.filter(attempts__started_at__gt=cycle_start)
        return cls._newest_first(reviews)

    @classmethod
    def _newest_first(cls, tasks: "TaskQuerySet") -> "Iterator[SelfReview]":
        rows = (
            tasks.filter(attempts__error="", attempts__result__has_key="review_verdict")
            .order_by("-attempts__pk")
            .values_list("pk", "attempts__result")
        )
        recorded = cast("Iterable[tuple[int, object]]", rows)
        return (review for task_pk, result in recorded if (review := cls.from_result(task_pk, result)) is not None)

    @property
    def is_hold(self) -> bool:
        return self.verdict == ReviewVerdict.Verdict.HOLD

    def rework_reason(self) -> str:
        header = (
            f"Self-review HOLD at {self.reviewed_sha or 'an unrecorded head'} (reviewing task {self.task_pk}): "
            "fix every finding below on this ticket branch, commit, run the tests. "
            "Testing and a fresh self-review follow automatically; do not open a PR."
        )
        return "\n".join([header, *self.rendered_findings(budget=_REASON_CAP - len(header) - _ELISION_ROOM)])

    def rendered_findings(self, *, budget: int) -> list[str]:
        """One block per finding within *budget* characters, ending with a pointer to whatever did not fit."""
        if not self.findings:
            return [f"- no findings returned; read reviewing task {self.task_pk}'s result"]
        lines: list[str] = []
        for index, held in enumerate(self.findings):
            block = held.render()
            budget -= len(block) + 1
            if budget < 0:
                elided = len(self.findings) - index
                lines.append(f"- {elided} more finding(s) elided; read reviewing task {self.task_pk}'s result")
                break
            lines.append(block)
        return lines


OPEN_PR_HINT = (
    "A pre-ship HOLD still routes its findings to a coding task on its own; this verb supersedes "
    "the ticket's active tasks, so it stays out of an open PR's delivery."
)


def _delivery_cycle_start(ticket: Ticket) -> datetime | None:
    """When the ticket last left a shipped state: self-reviews before it judged an earlier delivery."""
    shipped = Ticket.completable_states() | Ticket.merged_states()
    return (
        ticket.transitions.filter(from_state__in=shipped)
        .exclude(to_state__in=shipped)
        .order_by("-created_at", "-pk")
        .values_list("created_at", flat=True)
        .first()
    )
