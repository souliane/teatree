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
import traceback

from teatree.eval.backends import EvalRunner
from teatree.eval.models import EvalRun, EvalSpec
from teatree.llm.anthropic_limits import CreditExhaustedError

#: Raised by a worker that found the abort flag already set — a sibling scenario
#: exhausted the metered key's credit, so this one short-circuits BEFORE touching
#: the runner (no further scenario runs on the dead key).
_SUITE_ABORTED_MESSAGE = "suite aborted: a prior scenario exhausted the metered key's credit"

DEFAULT_PARALLEL = 1
MAX_PARALLEL = 20


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

    def _guarded(spec: EvalSpec) -> EvalRun:
        if aborted.is_set():
            raise CreditExhaustedError(_SUITE_ABORTED_MESSAGE)
        try:
            return _safe_run(runner, spec)
        except CreditExhaustedError:
            aborted.set()
            raise

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
