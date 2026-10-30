"""Bounded evidence for recurring and mixed attempt-failure bursts."""

import json
from collections import defaultdict
from operator import itemgetter

from teatree.core.modelkit.task_failure_taxonomy import is_causeless
from teatree.core.models import SelfImproveFiring, TaskAttempt, Ticket, TicketTransition

_WITNESSES = 2
_MAX_DISTINCT = 5
_ERROR_HEAD_LENGTH = 100


class AttemptBurstEvidence:
    @staticmethod
    def recurring_fingerprint(rows: list[dict], cause: str, task_ids: set[int]) -> tuple[str, str] | None:
        if is_causeless(cause):
            return None
        groups: dict[str, dict[int, dict]] = defaultdict(dict)
        for row in sorted(rows, key=itemgetter("epoch", "entity_id")):
            fingerprint = row.get("error_fingerprint")
            if (
                fingerprint
                and row.get("trace_id")
                and row.get("span_id")
                and row["cause"] == cause
                and row["task_id"] in task_ids
            ):
                groups[fingerprint][row["entity_id"]] = row
        recurring = [(fingerprint, attempts) for fingerprint, attempts in groups.items() if len(attempts) >= _WITNESSES]
        if not recurring:
            return None
        fingerprint, attempts = min(recurring, key=lambda item: (-len(item[1]), item[0]))
        witnesses = list(attempts.values())[-_WITNESSES:]
        citation = "\n".join(
            f"otel:{row['span_id']}\n{json.dumps(row, sort_keys=True, separators=(',', ':'))}" for row in witnesses
        )
        return fingerprint, citation

    @staticmethod
    def distinct_failures(rows: list[dict], cause: str, task_ids: set[int], errors: dict[int, str]) -> str:
        if is_causeless(cause):
            return ""
        seen: set[int] = set()
        entries: list[str] = []
        for row in sorted(rows, key=itemgetter("epoch", "entity_id")):
            attempt_id = row["entity_id"]
            if row["cause"] != cause or row["task_id"] not in task_ids or attempt_id in seen:
                continue
            seen.add(attempt_id)
            head = next(iter(errors.get(attempt_id, "").splitlines()), "")[:_ERROR_HEAD_LENGTH]
            label = f"attempt {attempt_id} (task {row['task_id']})"
            entries.append(f"{label}: {head}" if head else label)
            if len(entries) == _MAX_DISTINCT:
                break
        return "; ".join(entries)


def handled_by_finished_repair(firing: SelfImproveFiring, ids: list[int]) -> bool:
    repair = firing.ticket
    if repair is None or repair.state not in Ticket.finished_states():
        return False
    finished_at = None
    for state, created_at in (
        TicketTransition.objects.filter(ticket=repair)
        .order_by("-created_at", "-pk")
        .values_list("to_state", "created_at")
    ):
        if state not in Ticket.finished_states():
            break
        finished_at = created_at
    if finished_at is None:
        return False
    handled_ids: set[int] = set()
    for task_id, ended_at in TaskAttempt.objects.filter(task_id__in=ids).values_list("task_id", "ended_at"):
        if ended_at is None or ended_at > finished_at:
            return False
        handled_ids.add(task_id)
    return handled_ids == set(ids)
