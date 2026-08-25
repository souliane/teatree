"""The declared outcome contract every ``@task`` callable conforms to (#4528).

A queued job was recorded SUCCESSFUL whenever its callable returned without
raising. Teatree's task callables report failure by RETURNING it — ``{"ok":
False, ...}``, a non-zero ``exit_code``, a timed-out tick — so an async ship
whose push failed stamped a SUCCESSFUL row and the ticket read ``shipped`` with
nothing on the remote.

Enforced here rather than at either job boundary because
``queue_drain._run_one_ready_job``, the vendored ``db_worker.run_task`` and
``ImmediateBackend._execute_task`` all turn a raised exception into a FAILED
row: raising from the callable covers every runner, and ``Task.call()``, from
one place. ``functools.wraps`` keeps the wrapper transparent to django-tasks'
own validation — ``is_module_level_function`` reads ``__qualname__``,
``Task.module_path`` reads ``__module__``/``__qualname__``, and
``get_func_args`` goes through ``inspect.signature``, which follows
``__wrapped__``, so ``takes_context=True`` still validates.

The raise happens AFTER the callable returns, so a loops-queue chain that
re-enqueues its successor before returning still perpetuates itself. A test that
wants the body's raw mapping rather than the contract asks for
``<task>.func.__wrapped__(...)``.

``outcome`` is keyword-only and required: a callable whose result no policy can
read raises rather than passing as a success, and a NEW ``@task`` cannot be
added without choosing. ``tests/conformance/test_task_outcome_contract_walk.py``
is the AST guard that no module reaches past this one to ``django.tasks.task``.
"""

import functools
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from django.tasks import task as _django_task

if TYPE_CHECKING:
    from django.tasks import Task as DjangoTask

P = ParamSpec("P")
T = TypeVar("T")

#: The one ``TimerResult.action`` that actually ran a tick; the rest are no-ops.
_TICKED = "ticked"


class TaskOutcome(StrEnum):
    """How a ``@task`` callable's return value reports failure."""

    #: A mapping carrying ``ok`` (or ``skipped`` for an at-least-once no-op).
    OK_FLAG = "ok_flag"
    #: A mapping carrying ``exit_code`` — ``int`` or ``str``, nullable while unfinished.
    EXIT_CODE = "exit_code"
    #: A ``TimerResult``: only ``action == "ticked"`` carries ``timed_out``/``returncode``.
    TICK = "tick"
    #: A counts/report mapping with NO failure axis — only a raise fails it.
    REPORT = "report"


class TaskOutcomeError(RuntimeError):
    """A ``@task`` callable reported failure in its return value."""


class UnclassifiableTaskResultError(RuntimeError):
    """A ``@task`` callable returned something its declared policy cannot read."""


def _detail(result: Mapping[str, Any], fallback: str) -> str:
    reported = result.get("detail")
    return str(reported) if reported else fallback


def _classify_ok_flag(result: Mapping[str, Any]) -> str | None:
    if result.get("skipped"):
        return None
    if "ok" not in result:
        msg = f"OK_FLAG result carries neither 'ok' nor 'skipped': {result!r}"
        raise UnclassifiableTaskResultError(msg)
    return None if result["ok"] else _detail(result, "the callable reported ok=False")


def _classify_exit_code(result: Mapping[str, Any]) -> str | None:
    if result.get("skipped"):
        return None
    if "exit_code" not in result:
        msg = f"EXIT_CODE result carries neither 'exit_code' nor 'skipped': {result!r}"
        raise UnclassifiableTaskResultError(msg)
    code = result["exit_code"]
    # Nullable by contract: an attempt recorded but not yet finished is not a failure.
    if code is None:
        return None
    try:
        failed = int(code) != 0
    except (TypeError, ValueError) as exc:
        msg = f"EXIT_CODE result carries a non-numeric exit_code: {result!r}"
        raise UnclassifiableTaskResultError(msg) from exc
    return _detail(result, f"exit_code={code}") if failed else None


def _classify_tick(result: Mapping[str, Any]) -> str | None:
    if "action" not in result:
        msg = f"TICK result carries no 'action': {result!r}"
        raise UnclassifiableTaskResultError(msg)
    if result["action"] != _TICKED:
        return None
    loop = result.get("loop")
    if result.get("timed_out"):
        return f"loop {loop!r} tick timed out"
    returncode = result.get("returncode")
    if returncode in {0, None}:
        return None
    return f"loop {loop!r} tick exited {returncode}"


_CLASSIFIERS: dict[TaskOutcome, Callable[[Mapping[str, Any]], str | None]] = {
    TaskOutcome.OK_FLAG: _classify_ok_flag,
    TaskOutcome.EXIT_CODE: _classify_exit_code,
    TaskOutcome.TICK: _classify_tick,
}


def classify(result: object, outcome: TaskOutcome) -> str | None:
    """The failure detail *result* reports under *outcome*, or ``None`` for success."""
    if outcome is TaskOutcome.REPORT:
        return None
    if not isinstance(result, Mapping):
        msg = f"{outcome} expects a mapping, got {type(result).__name__}: {result!r}"
        raise UnclassifiableTaskResultError(msg)
    return _CLASSIFIERS[outcome](result)


def task(
    *,
    outcome: TaskOutcome,
    queue_name: str | None = None,
    takes_context: bool = False,
) -> Callable[[Callable[P, T]], "DjangoTask[P, T]"]:
    """``django.tasks.task``, with *outcome* declaring how the result reports failure."""

    def decorate(func: Callable[P, T]) -> "DjangoTask[P, T]":
        @functools.wraps(func)
        def enforce_outcome(*args: P.args, **kwargs: P.kwargs) -> T:
            result = func(*args, **kwargs)
            detail = classify(result, outcome)
            if detail is not None:
                raise TaskOutcomeError(detail)
            return result

        if queue_name is None:
            return _django_task(takes_context=takes_context)(enforce_outcome)
        return _django_task(queue_name=queue_name, takes_context=takes_context)(enforce_outcome)

    return decorate


__all__ = [
    "TaskOutcome",
    "TaskOutcomeError",
    "UnclassifiableTaskResultError",
    "classify",
    "task",
]
