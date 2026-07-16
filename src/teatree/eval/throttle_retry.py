"""Bounded transient-throttle retry envelope for the metered in-process eval runner.

Parallel scenarios share ONE OAuth token, so a rate-limit burst turns per-token
limits into false reds unless a run rides them out. :class:`ThrottleRetryDriver`
wraps one attempt-producing ``drive`` callable in a bounded retry loop: a TRANSIENT
throttle backs off exponentially (1, 2, 4, 8, 16s capped at 30s + jitter), a
SUSTAINED one waits the classified window clamped to a bounded cap, and the
empty-trajectory watchdog ``TimeoutError`` is ridden out too. A genuine cap, a
credit exhaustion, or a mislabeled success is graded by the caller's
:class:`ThrottleRetryHandlers` — NEVER retried, so a real behavioral fail is never
laundered into a passing retry. The runner-specific grading stays in the caller;
only the retry orchestration and backoff schedule live here.
"""

import dataclasses
import os
import random
import time
from collections.abc import Callable

from claude_agent_sdk import Message

from teatree.eval.api_errors import (
    THROTTLE_TERMINAL_PREFIX,
    SuccessMislabelResultError,
    TerminalResultError,
    ThrottleKind,
    ThrottleSignal,
    classify_transient_throttle,
)
from teatree.eval.models import EvalRun
from teatree.llm.anthropic_limits import CreditExhaustedError

#: Bounded retry envelope. A TRANSIENT throttle backs off exponentially (base ->
#: cap) plus jitter to de-correlate the concurrent retries on the shared token;
#: override the attempt count via ``T3_EVAL_THROTTLE_RETRIES`` (0 disables retry).
THROTTLE_RETRY_MAX_ATTEMPTS = 5
#: Watchdog ``TimeoutError`` retries are capped SEPARATELY and much lower than a
#: rate-limit throttle: a genuine hang re-hangs for the FULL watchdog on each
#: retry, so riding a timeout out on the large throttle budget would burn up to
#: ``(1 + THROTTLE_RETRY_MAX_ATTEMPTS)`` watchdog windows per scenario (~6x900s).
#: A timeout gets a few retries, then surfaces loud.
TIMEOUT_RETRY_MAX_ATTEMPTS = 2
_THROTTLE_RETRIES_ENV_VAR = "T3_EVAL_THROTTLE_RETRIES"
THROTTLE_BACKOFF_BASE_SECONDS = 1.0
THROTTLE_BACKOFF_CAP_SECONDS = 30.0
THROTTLE_BACKOFF_JITTER_SECONDS = 1.0
#: A SUSTAINED (rolling subscription-window) wait is bounded so a genuine ~5h
#: session cap SURFACES loud after a finite wait instead of hanging forever; the
#: classified window horizon (hours) is clamped down to this cap.
THROTTLE_WINDOW_WAIT_MAX_SECONDS = 600.0
#: A system RNG for the backoff jitter so concurrent workers on the shared token
#: draw independent jitter (de-correlating retries, avoiding a thundering herd); a
#: test injects a deterministic ``rand`` in its place.
_JITTER_RNG = random.SystemRandom()
#: A watchdog ``TimeoutError`` leaves an EMPTY trajectory (``asyncio.wait_for``
#: discards the partial run on cancel) — the infra-hang signature, ridden out as a
#: TRANSIENT throttle.
_TIMEOUT_THROTTLE = ThrottleSignal(kind=ThrottleKind.TRANSIENT, cause=None, wait_seconds=None)


def resolve_throttle_retries() -> int:
    """The bounded retry attempt count, ``T3_EVAL_THROTTLE_RETRIES`` overriding the default.

    A missing/empty/unparsable value yields the default; an explicit ``0`` is
    honored (disables the retry) — only a negative value falls back.
    """
    raw = os.environ.get(_THROTTLE_RETRIES_ENV_VAR, "").strip()
    if not raw:
        return THROTTLE_RETRY_MAX_ATTEMPTS
    try:
        value = int(raw)
    except ValueError:
        return THROTTLE_RETRY_MAX_ATTEMPTS
    return value if value >= 0 else THROTTLE_RETRY_MAX_ATTEMPTS


THROTTLE_MAX_ATTEMPTS = resolve_throttle_retries()


def throttle_reason(signal: ThrottleSignal, attempts: int) -> str:
    """The terminal reason for a throttle that outlasted its bounded retry budget.

    Names the throttle so an exhausted rate limit surfaces loud as a distinct,
    honest red — never mislabeled as a behavioral fail.
    """
    label = signal.cause.value if signal.cause is not None else signal.kind.value
    return f"{THROTTLE_TERMINAL_PREFIX} {label} (exhausted {attempts} retries)"


#: The terminal reason surfaced when a watchdog timeout outlasts its own retry
#: cap. Kept as the bare ``"timeout"`` (a member of ``CAP_TERMINAL_REASONS``) so a
#: hung run classifies exactly as before — #9 caps the retry COUNT, it does not
#: change how an exhausted timeout is graded.
_TIMEOUT_TERMINAL_REASON = "timeout"


@dataclasses.dataclass(frozen=True)
class ThrottleRetryHandlers:
    """The caller's per-terminus graders — how each outcome becomes an ``EvalRun``.

    ``grade_success`` takes the captured messages and the retry count (so the run
    carries ``throttle_retries``); ``grade_cap`` / ``grade_mislabel`` grade a genuine
    cap / mislabeled success without retry; ``surface_throttled`` builds the loud red
    for a throttle that outlasted the retry budget.
    """

    grade_success: Callable[[list[Message], int], EvalRun]
    grade_cap: Callable[[TerminalResultError], EvalRun]
    grade_mislabel: Callable[[SuccessMislabelResultError], EvalRun]
    surface_throttled: Callable[[str, int], EvalRun]


@dataclasses.dataclass(frozen=True)
class ThrottleRetryDriver:
    """Drive an attempt-producing callable with bounded transient-throttle retry.

    ``sleep`` and ``rand`` are injectable so the backoff schedule is testable
    without real time or a real RNG. ``timeout_max_attempts`` bounds watchdog
    timeouts on their OWN small budget, separate from the rate-limit throttle
    budget ``max_attempts``.
    """

    max_attempts: int
    timeout_max_attempts: int = TIMEOUT_RETRY_MAX_ATTEMPTS
    sleep: Callable[[float], None] = time.sleep
    rand: Callable[[], float] = _JITTER_RNG.random

    def run(self, drive: Callable[[], list[Message]], handlers: ThrottleRetryHandlers) -> EvalRun:
        attempt = 0
        timeout_attempt = 0
        while True:
            try:
                messages = drive()
            except CreditExhaustedError:
                # A $0 metered key is terminal for the WHOLE suite, not a per-run
                # throttle — propagate so the caller aborts (never retried).
                raise
            except TerminalResultError as cap:
                # A GENUINE behavioral cap (budget/max_turns). Retrying it would hide
                # a real fail behind a backoff, so it is graded, never retried.
                return handlers.grade_cap(cap)
            except SuccessMislabelResultError as mislabel:
                return handlers.grade_mislabel(mislabel)
            except TimeoutError:
                # A watchdog timeout leaves an EMPTY trajectory (asyncio.wait_for
                # discards the partial run on cancel). Ride it out on its OWN small
                # budget — a genuine hang re-hangs for the full watchdog each retry,
                # so it must NOT share the large rate-limit budget.
                if timeout_attempt >= self.timeout_max_attempts:
                    return handlers.surface_throttled(_TIMEOUT_TERMINAL_REASON, attempt + timeout_attempt)
                self.sleep(self._delay(_TIMEOUT_THROTTLE, timeout_attempt))
                timeout_attempt += 1
                continue
            except Exception as exc:
                signal = classify_transient_throttle(str(exc))
                if signal is None:
                    raise  # a non-throttle is a genuine crash — preserve its red
                reason = throttle_reason(signal, attempt)
                next_attempt = self._next_attempt(signal, attempt)
                if next_attempt is None:
                    return handlers.surface_throttled(reason, attempt)
                attempt = next_attempt
                continue
            return handlers.grade_success(messages, attempt + timeout_attempt)

    def _next_attempt(self, signal: ThrottleSignal, attempt: int) -> int | None:
        """Sleep the backoff and return the next attempt index, or ``None`` when the budget is spent."""
        if attempt >= self.max_attempts:
            return None
        self.sleep(self._delay(signal, attempt))
        return attempt + 1

    def _delay(self, signal: ThrottleSignal, attempt: int) -> float:
        """A bounded window wait for SUSTAINED, else jittered exponential backoff."""
        if signal.kind is ThrottleKind.SUSTAINED:
            window = signal.wait_seconds if signal.wait_seconds is not None else THROTTLE_WINDOW_WAIT_MAX_SECONDS
            return min(window, THROTTLE_WINDOW_WAIT_MAX_SECONDS)
        backoff = min(THROTTLE_BACKOFF_BASE_SECONDS * (2**attempt), THROTTLE_BACKOFF_CAP_SECONDS)
        return backoff + self.rand() * THROTTLE_BACKOFF_JITTER_SECONDS
