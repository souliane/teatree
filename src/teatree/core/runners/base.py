from abc import ABC, abstractmethod
from dataclasses import dataclass

from teatree.core.models import Ticket


@dataclass(frozen=True, slots=True)
class RunnerResult:
    ok: bool
    detail: str = ""


class RunnerBase(ABC):
    """Base for composable transition runners.

    Subclasses hold a reference to their ``Ticket`` and expose a ``run()`` that
    performs the I/O. Workers invoke ``run()`` after claiming the ticket with
    ``select_for_update()``; on success the worker advances the FSM.
    """

    def __init__(self, ticket: Ticket) -> None:
        self.ticket = ticket

    @abstractmethod
    def run(self) -> RunnerResult:
        raise NotImplementedError
