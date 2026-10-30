"""Ask once about aged parked tickets and consume an owner retirement answer."""

from collections.abc import Callable
from dataclasses import dataclass

from teatree.core.factory.parked_ticket_decision import (
    apply_retire_answers,
    ask_owner_about_parked,
    awaiting_decision,
    release_undecided_answers,
    settle_changed_parked_cards,
)
from teatree.core.factory.parked_tickets import aged_parked_tasks
from teatree.core.models import Task
from teatree.loop.scanners.base import ScanSignal


@dataclass(slots=True)
class ParkedTicketQuestionScanner:
    name: str = "parked_ticket_question"
    tasks: Callable[[], list[Task]] = aged_parked_tasks

    def scan(self) -> list[ScanSignal]:
        release_undecided_answers()
        aged = self.tasks()
        if apply_retire_answers(aged):
            aged = self.tasks()
        awaiting = awaiting_decision(aged)
        settle_changed_parked_cards(awaiting)
        ask_owner_about_parked(awaiting)
        return []
