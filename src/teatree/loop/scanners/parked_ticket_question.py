"""Ask once about aged parked tickets and consume an owner retirement answer."""

from collections.abc import Callable
from dataclasses import dataclass

from teatree.core.factory.parked_tickets import (
    KEEP_ANSWER,
    PARKED_MARKER_PREFIX,
    RETIRE_ANSWER,
    aged_parked_tasks,
    ask_owner_about_parked,
    owner_ruled_pairs,
    retire_parked,
    task_pair,
)
from teatree.core.models import Task
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.scanners.base import ScanSignal


@dataclass(slots=True)
class ParkedTicketQuestionScanner:
    name: str = "parked_ticket_question"
    tasks: Callable[[], list[Task]] = aged_parked_tasks

    def scan(self) -> list[ScanSignal]:
        aged = self.tasks()
        answered = list(
            DeferredQuestion.objects.filter(dedupe_marker__startswith=PARKED_MARKER_PREFIX, answered_at__isnull=False)
        )
        retire = {pair for question in answered for pair in owner_ruled_pairs(question, RETIRE_ANSWER)}
        keep = {pair for question in answered for pair in owner_ruled_pairs(question, KEEP_ANSWER)}
        retire_parked(retire, aged)
        if DeferredQuestion.owner_pending().filter(dedupe_marker__startswith=PARKED_MARKER_PREFIX).exists():
            return []
        ruled = retire | keep
        ask_owner_about_parked([task for task in aged if task_pair(task) not in ruled])
        return []
