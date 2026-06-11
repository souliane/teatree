"""Bounded concurrent driver over the per-scenario eval runner.

The metered ``sdk`` lane drives one in-process Agent-SDK query per scenario and the
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
must not lose the other 165's results.
"""

import concurrent.futures
import traceback

from teatree.eval.backends import EvalRunner
from teatree.eval.models import EvalRun, EvalSpec

DEFAULT_PARALLEL = 1
MAX_PARALLEL = 20


def run_specs(runner: EvalRunner, specs: list[EvalSpec], *, parallel: int = DEFAULT_PARALLEL) -> list[EvalRun]:
    """Run each spec through *runner*, ``parallel`` at a time, in spec order.

    ``parallel=1`` (default) runs strictly sequentially — identical to the
    pre-existing ``[runner.run(s) for s in specs]`` loop. ``parallel>1`` caps
    concurrent in-flight runs at ``min(parallel, len(specs), MAX_PARALLEL)``.
    """
    if not specs:
        return []
    workers = max(1, min(parallel, len(specs), MAX_PARALLEL))
    if workers == 1:
        return [_safe_run(runner, spec) for spec in specs]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda spec: _safe_run(runner, spec), specs))


def _safe_run(runner: EvalRunner, spec: EvalSpec) -> EvalRun:
    try:
        return runner.run(spec)
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
