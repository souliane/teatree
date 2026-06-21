"""Per-step timeout guard for provision workflows (#TODO-7).

Provides a time-boxed execution wrapper for individual provision steps,
preventing indefinite hangs during operations like DB imports or overlays
that may stall without progress signals.
"""

import logging
import signal
from collections.abc import Generator
from contextlib import contextmanager

from teatree.config import get_effective_settings

logger = logging.getLogger(__name__)

DEFAULT_PROVISION_STEP_TIMEOUT_SECONDS = 300


class ProvisionTimeoutError(TimeoutError):
    """Raised when a provision step exceeds its configured timeout."""

    def __init__(self, step_name: str, timeout_seconds: int) -> None:
        self.step_name = step_name
        self.timeout_seconds = timeout_seconds
        msg = f"Provision step '{step_name}' timed out after {timeout_seconds}s"
        super().__init__(msg)


def resolve_provision_step_timeout_seconds() -> int:
    """Resolve the configured timeout (seconds) for a single provision step.

    Reads ``provision_step_timeout_seconds`` off effective settings (per-overlay
    override → global → default). Always returns a positive timeout — a
    non-positive or unreadable value degrades to the default so a
    misconfiguration can never disable the time-box.
    """
    settings_ = get_effective_settings()
    value = getattr(settings_, "provision_step_timeout_seconds", DEFAULT_PROVISION_STEP_TIMEOUT_SECONDS)
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PROVISION_STEP_TIMEOUT_SECONDS
    return timeout if timeout > 0 else DEFAULT_PROVISION_STEP_TIMEOUT_SECONDS


def _timeout_handler(step_name: str, timeout_seconds: int) -> None:
    """Signal handler invoked on timeout — raises ProvisionTimeoutError."""
    raise ProvisionTimeoutError(step_name, timeout_seconds)


@contextmanager
def timeout_provision_step(
    step_name: str,
    timeout_seconds: int | None = None,
) -> Generator[None]:
    """Context manager that times out a provision step after *timeout_seconds*.

    Raises ``ProvisionTimeoutError`` when the step exceeds the timeout. If
    *timeout_seconds* is ``None``, uses :func:`resolve_provision_step_timeout_seconds`.

    Example:
        >>> with timeout_provision_step("db-import", timeout_seconds=300):
        ...     overlay.db_import(worktree)
    """
    if timeout_seconds is None:
        timeout_seconds = resolve_provision_step_timeout_seconds()

    def _alarm_handler(_signum: int, _frame: object) -> None:
        _timeout_handler(step_name, timeout_seconds)

    old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(timeout_seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
