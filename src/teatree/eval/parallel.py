"""Bounded concurrent driver over the per-scenario eval runner.

The metered ``api`` lane drives one in-process Agent-SDK query per scenario and the
suite runs them SEQUENTIALLY — so wall-clock is N x per-scenario latency (~82s
each x ~166 scenarios = hours). Each subprocess is almost entirely I/O-bound
(network round-trips to the model), so running several at once is a near-linear
wall-clock win with no extra per-scenario cost.

:func:`run_specs` wraps any :class:`~teatree.eval.backends.EvalRunner` in a
bounded :class:`~concurrent.futures.ThreadPoolExecutor`. It does NOT touch the
runner internals — it calls the existing ``runner.run(spec)`` — so the
subprocess/transcript machinery is unchanged and a serial run (``parallel=1``,
the default) is byte-for-byte today's behaviour. Results come back in spec order
regardless of completion order, so callers still ``zip`` runs to specs.

A scenario whose ``run`` raises (an unexpected runner crash) is captured as an
errored :class:`EvalRun` rather than aborting the whole pool — one bad scenario
must not lose the other 165's results. The ONE exception is a
:class:`~teatree.llm.anthropic_limits.CreditExhaustedError`: a $0 metered key is
terminal for the WHOLE suite (every remaining scenario would red identically on
the dead key), so it is NOT swallowed into per-scenario reds — it propagates past
:func:`_safe_run` and ABORTS the run, cancelling any not-yet-started scenarios.
"""

import concurrent.futures
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from teatree.eval.backends import EvalRunner
from teatree.eval.models import EvalRun, EvalSpec
from teatree.llm.anthropic_limits import CreditExhaustedError

#: Raised by a worker that found the abort flag already set — a sibling scenario
#: exhausted the metered key's credit, so this one short-circuits BEFORE touching
#: the runner (no further scenario runs on the dead key).
_SUITE_ABORTED_MESSAGE = "suite aborted: a prior scenario exhausted the metered key's credit"

DEFAULT_PARALLEL = 1
MAX_PARALLEL = 20

#: AIMD concurrency-governor tuning. A throttle event MULTIPLICATIVELY halves the
#: permit ceiling toward :data:`CONCURRENCY_FLOOR`; :data:`GROW_AFTER_CLEARS`
#: consecutive clean completions ADDITIVELY grow it one step back toward the worker
#: count. :data:`SHRINK_COOLDOWN_SECONDS` keeps a burst of near-simultaneous
#: throttles from collapsing the ceiling in a single step.
CONCURRENCY_FLOOR = 1
GROW_AFTER_CLEARS = 3
SHRINK_COOLDOWN_SECONDS = 5.0


class ConcurrencyGovernor:
    """An AIMD ceiling over the concurrent in-flight runs, shared across the pool.

    Standard congestion control for the metered lane's ONE shared OAuth token: the
    permit ceiling starts at the worker count and each :meth:`slot` acquisition
    blocks while the in-flight count is already at the ceiling. A throttle event (a
    run that rode out >=1 Layer-2 retry, i.e. ``throttle_retries > 0``)
    MULTIPLICATIVELY halves the ceiling toward :data:`CONCURRENCY_FLOOR`;
    :data:`GROW_AFTER_CLEARS` consecutive clean completions ADDITIVELY grow it one
    step back toward the worker count. A genuine (non-throttle) error is neutral —
    it neither shrinks nor grows. The floor of 1 guarantees forward progress, so
    the governor can never deadlock the pool.
    """

    def __init__(
        self,
        workers: int,
        *,
        floor: int = CONCURRENCY_FLOOR,
        grow_after: int = GROW_AFTER_CLEARS,
        cooldown: float = SHRINK_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._workers = workers
        self._floor = max(1, min(floor, workers))
        self._grow_after = grow_after
        self._cooldown = cooldown
        self._clock = clock
        self._limit = workers
        self._active = 0
        self._clean_streak = 0
        self._last_shrink = float("-inf")
        self._cond = threading.Condition()

    @property
    def limit(self) -> int:
        with self._cond:
            return self._limit

    @contextmanager
    def slot(self) -> Iterator[None]:
        with self._cond:
            while self._active >= self._limit:
                self._cond.wait()
            self._active += 1
        try:
            yield
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify_all()

    def record_completion(self, run: EvalRun) -> None:
        """Feed a finished run's throttle signal into the ceiling: shrink, grow, or neutral."""
        if run.throttle_retries > 0:
            self._shrink()
        elif not run.is_error:
            self._grow()

    def _shrink(self) -> None:
        with self._cond:
            now = self._clock()
            if now - self._last_shrink < self._cooldown:
                return
            self._last_shrink = now
            self._clean_streak = 0
            self._limit = max(self._floor, self._limit // 2)
            self._cond.notify_all()

    def _grow(self) -> None:
        with self._cond:
            self._clean_streak += 1
            if self._clean_streak < self._grow_after:
                return
            self._clean_streak = 0
            if self._limit < self._workers:
                self._limit += 1
                self._cond.notify_all()


def run_specs(runner: EvalRunner, specs: list[EvalSpec], *, parallel: int = DEFAULT_PARALLEL) -> list[EvalRun]:
    """Run each spec through *runner*, ``parallel`` at a time, in spec order.

    ``parallel=1`` (default) runs strictly sequentially — identical to the
    pre-existing ``[runner.run(s) for s in specs]`` loop. ``parallel>1`` caps
    concurrent in-flight runs at ``min(parallel, len(specs), MAX_PARALLEL)``.

    A :class:`~teatree.llm.anthropic_limits.CreditExhaustedError` from any
    scenario ABORTS the whole run (it propagates instead of becoming N reds): the
    serial loop stops on the raise, and the parallel pool cancels every
    not-yet-started future so no further scenario runs on the dead key.
    """
    if not specs:
        return []
    workers = max(1, min(parallel, len(specs), MAX_PARALLEL))
    if workers == 1:
        return [_safe_run(runner, spec) for spec in specs]

    # A shared flag, set by the first scenario to exhaust the metered key. A
    # worker that picks up a queued scenario after the flag is set short-circuits
    # BEFORE calling the runner — so no further scenario runs on the dead key even
    # though every spec was submitted to the pool up front (workers pull from the
    # queue faster than the consumer could cancel them otherwise).
    aborted = threading.Event()
    # An AIMD governor over the shared OAuth token: each worker acquires a slot,
    # and a throttled completion shrinks the ceiling so the suite backs its
    # parallel load off the token (grown back on clean completions).
    governor = ConcurrencyGovernor(workers)

    def _guarded(spec: EvalSpec) -> EvalRun:
        if aborted.is_set():
            raise CreditExhaustedError(_SUITE_ABORTED_MESSAGE)
        try:
            with governor.slot():
                run = _safe_run(runner, spec)
        except CreditExhaustedError:
            aborted.set()
            raise
        governor.record_completion(run)
        return run

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_guarded, spec): index for index, spec in enumerate(specs)}
        completed: dict[int, EvalRun] = {}
        try:
            for future in concurrent.futures.as_completed(futures):
                completed[futures[future]] = future.result()
        except CreditExhaustedError:
            for pending in futures:
                pending.cancel()
            raise
        return [completed[index] for index in range(len(specs))]


def _safe_run(runner: EvalRunner, spec: EvalSpec) -> EvalRun:
    try:
        return runner.run(spec)
    except CreditExhaustedError:
        # A $0 metered key is terminal for the WHOLE suite, not one scenario:
        # propagate so run_specs aborts the run instead of redding every
        # remaining scenario identically behind the dead key.
        raise
    except Exception as exc:  # noqa: BLE001 — one scenario's crash must not abort the suite.
        return EvalRun(
            spec_name=spec.name,
            tool_calls=(),
            text_blocks=(),
            terminal_reason=f"runner error: {type(exc).__name__}: {exc}",
            is_error=True,
            raw_stdout="",
            raw_stderr=traceback.format_exc(),
        )
