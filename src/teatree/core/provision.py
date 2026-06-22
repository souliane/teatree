"""Signal-based timeout guard for long-blocking callable-based provision steps.

Callable-based steps (e.g., ``db_import``, overlay installations, migrations)
can hang indefinitely without progress output. This module wraps such steps
with a configurable timeout ceiling so they never silently grind.

Unlike subprocess-based timeouts (subprocess.TimeoutExpired), the
signal-based approach here works for Python callables that block in C/extension
code, allowing the timeout to fire even when a long-running operation is
executing native code.

Configuration: the timeout ceiling is tunable via
``[teatree] provision_step_timeout_seconds`` (per-overlay overridable, global
fallback, or :data:`DEFAULT_STEP_TIMEOUT_SECONDS`). A non-positive or
unparsable value always degrades to the default, so misconfiguration can
never disable the time-box — the "never hang" invariant is non-negotiable.
"""

import signal
import types
from collections.abc import Iterator
from contextlib import contextmanager

from teatree.config import get_effective_settings

DEFAULT_STEP_TIMEOUT_SECONDS = 1800


class ProvisionTimeoutError(RuntimeError):
    """Raised when a provision step exceeds its configured timeout."""

    def __init__(self, step_name: str, timeout_seconds: int) -> None:
        self.step_name = step_name
        self.timeout_seconds = timeout_seconds
        super().__init__(f"Step {step_name!r} timed out after {timeout_seconds}s")


def resolve_provision_step_timeout_seconds() -> int:
    """The configured hard ceiling (seconds) for one callable provision step.

    Reads ``provision_step_timeout_seconds`` off the effective settings
    (per-overlay override → global → :data:`DEFAULT_STEP_TIMEOUT_SECONDS`).
    Always returns a positive ceiling — a non-positive or unreadable value
    degrades to the default so a misconfiguration can never disable the
    time-box.
    """
    settings_ = get_effective_settings()
    value = getattr(settings_, "provision_step_timeout_seconds", DEFAULT_STEP_TIMEOUT_SECONDS)
    try:
        ceiling = int(value)
    except (TypeError, ValueError):
        return DEFAULT_STEP_TIMEOUT_SECONDS
    return ceiling if ceiling > 0 else DEFAULT_STEP_TIMEOUT_SECONDS


@contextmanager
def timeout_provision_step(step_name: str, timeout: int | None = None) -> Iterator[None]:
    """Context manager that enforces a timeout on a provision step via SIGALRM.

    When the context exits normally before timeout, the alarm is cancelled.
    If the timeout fires, it raises ProvisionTimeoutError. The step name is
    included in the error for logging and diagnostics.

    Args:
        step_name: Name of the provision step (for error messages).
        timeout: Timeout in seconds. If None, uses resolve_provision_step_timeout_seconds().
    """
    ceiling = timeout if timeout is not None else resolve_provision_step_timeout_seconds()

    def _timeout_handler(_signum: int, _frame: types.FrameType | None) -> None:
        raise ProvisionTimeoutError(step_name, ceiling)

    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(ceiling)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


__all__ = [
    "DEFAULT_STEP_TIMEOUT_SECONDS",
    "ProvisionTimeoutError",
    "resolve_provision_step_timeout_seconds",
    "timeout_provision_step",
]
