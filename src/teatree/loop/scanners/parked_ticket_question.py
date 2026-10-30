"""Ask once about aged parked tickets and consume an owner retirement answer."""

from collections.abc import Callable
from dataclasses import dataclass

from teatree.core.factory.parked_tickets import (
    PARKED_MARKER_PREFIX,
    aged_parked_tasks,
    ask_owner_about_parked,
    retire_answered_parked,
)
from teatree.core.models import Task
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.scanners.base import ScanSignal


@dataclass(slots=True)
class ParkedTicketQuestionScanner:
    name: str = "parked_ticket_question"
    tasks: Callable[[], list[Task]] = aged_parked_tasks

    def scan(self) -> list[ScanSignal]:
        answered = DeferredQuestion.objects.filter(
            dedupe_marker__startswith=PARKED_MARKER_PREFIX, answered_at__isnull=False
        )
        for question in answered:
            retire_answered_parked(question)
        if DeferredQuestion.owner_pending().filter(dedupe_marker__startswith=PARKED_MARKER_PREFIX).exists():
            return []
        ask_owner_about_parked(self.tasks())
        return []
