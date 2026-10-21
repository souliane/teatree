"""The running holder's checkout exit: settled once before its terminal write, consumed by the release."""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.core.models.task import Task


@dataclass(frozen=True)
class CheckoutExit:
    release: bool
    attested_state: str | None = None


@dataclass
class CheckoutExitScope:
    prepare: Callable[["Task", bool, bool], CheckoutExit | None]
    settled: CheckoutExit | None = None
    settled_for: tuple[int, bool, bool] | None = None
    park_attests: bool = False
    verified_run: bool = False


_checkout_exit: ContextVar[CheckoutExitScope | None] = ContextVar("checkout_exit", default=None)


@contextmanager
def checkout_exit_scope(prepare: Callable[["Task", bool, bool], CheckoutExit | None]) -> Generator[CheckoutExitScope]:
    scope = CheckoutExitScope(prepare)
    token = _checkout_exit.set(scope)
    try:
        yield scope
    finally:
        _checkout_exit.reset(token)


def prepared_checkout_exit(task: "Task", *, attest: bool = True, wait: bool = True) -> CheckoutExit | None:
    scope = _checkout_exit.get()
    if scope is None:
        return None
    if scope.settled_for != (task.pk, attest, wait):
        scope.settled, scope.settled_for = scope.prepare(task, attest, wait), (task.pk, attest, wait)
    return scope.settled


def forget_checkout_exit() -> None:
    if (scope := _checkout_exit.get()) is not None:
        scope.settled = None
        scope.settled_for = None


def attest_checkout_on_park() -> None:
    """Mark the running holder's coming park as an own exit whose checkout state it may attest."""
    if (scope := _checkout_exit.get()) is not None:
        scope.park_attests = True


def mark_verified_checkout_run() -> None:
    if (scope := _checkout_exit.get()) is not None:
        scope.verified_run = True
