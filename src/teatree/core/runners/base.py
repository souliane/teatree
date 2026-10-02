from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RunnerResult:
    ok: bool
    detail: str = ""


class RunnerBase(ABC):
    """Base for composable transition runners.

    Subclasses bind whichever model row they operate on (ticket, worktree)
    via their own ``__init__`` and expose a ``run()`` that performs the I/O.
    Workers invoke ``run()`` after taking a row lock with
    ``select_for_update()``; on success the worker advances the FSM.
    """

    @abstractmethod
    def run(self) -> RunnerResult:
        raise NotImplementedError
