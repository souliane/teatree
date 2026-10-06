"""An author's pre-PR self-review verdict, read back from the reviewing attempt that returned it.

No pull request exists yet, so no ``ReviewVerdict`` row can hold it: ``TaskAttempt.result``
already persists the returned ``review_verdict``, and the newest one governs.
"""

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

from teatree.core.models.errors import SelfReviewReworkRefusedError
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.review_verdict import Finding, ReviewVerdict
from teatree.core.models.ticket import Ticket

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from teatree.core.models.task import Task

_SUMMARY_CAP = 600
_REASON_CAP = 16_000
_ELISION_ROOM = 120


@dataclass(frozen=True, slots=True)
class SelfReview:
    task_pk: int
    verdict: str
    reviewed_sha: str
    findings: tuple[Finding, ...]

    @classmethod
    def from_result(cls, task_pk: int, result: object) -> "SelfReview | None":
        returned = result.get("review_verdict") if isinstance(result, dict) else None
        if not isinstance(returned, dict):
            return None
        findings = returned.get("findings")
        return cls(
            task_pk=task_pk,
            verdict=str(returned.get("verdict", "")).strip().lower(),
            reviewed_sha=str(returned.get("reviewed_sha") or "").strip().lower(),
            findings=tuple(Finding.from_dict(row) for row in findings if isinstance(row, dict))
            if isinstance(findings, list)
            else (),
        )

    @classmethod
    def of_task(cls, task: "Task") -> "SelfReview | None":
        attempt = task.attempts.filter(error="").order_by("-pk").first()
        return cls.from_result(task.pk, attempt.result) if attempt is not None else None

    @classmethod
    def latest_for(cls, ticket: Ticket) -> "SelfReview | None":
        return next(cls._newest_first(ticket), None)

    @classmethod
    def hold_count(cls, ticket: Ticket, *, excluding: int) -> int:
        return sum(1 for review in cls._newest_first(ticket) if review.is_hold and review.task_pk != excluding)

    @classmethod
    def held_for_rework(cls, ticket: Ticket) -> "SelfReview":
        """The HOLD a ticket parked past it still owes its rework, else refuse naming why."""
        if ticket.role != Ticket.Role.AUTHOR:
            msg = f"ticket {ticket.pk} is a reviewer ticket; only an author's own self-review holds its branch"
            raise SelfReviewReworkRefusedError(msg, hint="Run rework-hold on the author ticket that owns the branch.")
        if ticket.state not in {Ticket.State.TESTED, Ticket.State.SELF_REVIEWED}:
            msg = f"ticket {ticket.pk} is {ticket.state}; a HOLD is reworked only at tested or self_reviewed"
            raise SelfReviewReworkRefusedError(msg, hint="Let the ticket reach its self-review first.")
        if PullRequest.objects.live().filter(ticket=ticket).exists():
            msg = f"ticket {ticket.pk} has an open pull request, so its PR-keyed review gates it"
            raise SelfReviewReworkRefusedError(msg, hint="Push the fixes to the open pull request instead.")
        review = cls.latest_for(ticket)
        if review is None or not review.is_hold:
            msg = f"ticket {ticket.pk}'s latest self-review is not a HOLD; there is nothing to rework"
            raise SelfReviewReworkRefusedError(msg, hint="A merge_safe self-review lets the ticket ship as it is.")
        return review

    @classmethod
    def _newest_first(cls, ticket: Ticket) -> "Iterator[SelfReview]":
        if ticket.role != Ticket.Role.AUTHOR:
            return iter(())
        rows = (
            ticket.tasks.completed_in_phase("reviewing")
            .filter(attempts__error="", attempts__result__has_key="review_verdict")
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
        lines = [header]
        if not self.findings:
            lines.append(f"- no findings returned; read reviewing task {self.task_pk}'s result")
        used = len(header)
        for index, finding in enumerate(self.findings):
            line = f"- {replace(finding, summary=_clip(finding.summary)).describe()}"
            if used + len(line) + 1 > _REASON_CAP - _ELISION_ROOM:
                elided = len(self.findings) - index
                lines.append(f"- {elided} more finding(s) elided; read reviewing task {self.task_pk}'s result")
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)


def _clip(summary: str) -> str:
    return summary if len(summary) <= _SUMMARY_CAP else f"{summary[: _SUMMARY_CAP - 1]}…"
