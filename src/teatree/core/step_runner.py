"""Structured step execution with error propagation and reporting.

Replaces the fire-and-forget ``partial(subprocess.run, ..., check=False)``
pattern with explicit success/failure tracking per step.
"""

import logging
import subprocess  # noqa: S404 — only TimeoutExpired/CompletedProcess accessed, no shelling
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from teatree.utils.run import run_allowed_to_fail

if TYPE_CHECKING:
    from teatree.types import ProvisionStep

logger = logging.getLogger(__name__)

_PROVISION_TIMEBOX_MODULE = "teatree.core.provision_timebox"

# Fallback hard ceiling (seconds) for the degraded plain-subprocess path, which
# runs only when ``provision_timebox`` is ABSENT and so cannot consult its
# configurable ``resolve_step_timeout_seconds()``. Matches that module's
# ``DEFAULT_STEP_TIMEOUT_SECONDS`` so a ``timeout=None`` step still aborts on a
# ceiling instead of hanging forever — the "never hang" invariant the time-box
# exists for (souliane/teatree#2220) holds on the degraded path too.
_PLAIN_STEP_TIMEOUT_FALLBACK_SECONDS = 1800


@dataclass(frozen=True, slots=True)
class StepResult:
    """Outcome of a single provisioning step."""

    name: str
    success: bool
    duration: float = 0.0
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    required: bool = True
    skipped: bool = False

    def summary(self) -> str:
        status = "SKIP" if self.skipped else ("OK" if self.success else "FAILED")
        msg = f"  [{status}] {self.name} ({self.duration:.1f}s)"
        if not self.success and self.error:
            msg += f"\n         {self.error}"
        return msg

    def to_dict(self) -> "StepResultDict":
        return {
            "name": self.name,
            "success": self.success,
            "duration": self.duration,
            "error": self.error,
            "required": self.required,
            "skipped": self.skipped,
        }


class StepResultDict(TypedDict):
    """JSON-serializable projection of :class:`StepResult` for ``Worktree.extra`` persistence.

    Deliberately narrower than the full dataclass — ``stdout``/``stderr`` can be
    arbitrarily large subprocess output and add nothing to a persisted report
    a human or the ``--report`` table reads later.
    """

    name: str
    success: bool
    duration: float
    error: str
    required: bool
    skipped: bool


class ProvisionReportDict(TypedDict):
    """JSON-serializable projection of :class:`ProvisionReport` (``Worktree.extra['provision_report']``)."""

    steps: list[StepResultDict]
    total_duration: float
    success: bool


@dataclass
class ProvisionReport:
    """Aggregated outcome of a multi-step provisioning run."""

    steps: list[StepResult] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not any(s.required and not s.success for s in self.steps)

    @property
    def total_duration(self) -> float:
        return sum(s.duration for s in self.steps)

    @property
    def slowest_step(self) -> StepResult | None:
        return max(self.steps, key=lambda s: s.duration, default=None)

    @property
    def failed_step(self) -> str | None:
        for s in self.steps:
            if not s.success:
                return s.name
        return None

    @property
    def failed_required_step(self) -> str | None:
        for s in self.steps:
            if s.required and not s.success:
                return s.name
        return None

    def summary(self) -> str:
        lines = [s.summary() for s in self.steps]
        total = len(self.steps)
        ok = sum(1 for s in self.steps if s.success)
        lines.append(f"\n  {ok}/{total} steps succeeded. Total: {self.total_duration:.1f}s")
        if not self.success:
            lines.append(f"  First failure: {self.failed_step}")
        return "\n".join(lines)

    def to_dict(self) -> ProvisionReportDict:
        return {
            "steps": [s.to_dict() for s in self.steps],
            "total_duration": self.total_duration,
            "success": self.success,
        }

    @classmethod
    def from_dict(cls, data: ProvisionReportDict) -> "ProvisionReport":
        steps = [
            StepResult(
                name=str(s.get("name", "")),
                success=bool(s.get("success", False)),
                duration=float(s.get("duration", 0.0)),
                error=str(s.get("error", "")),
                required=bool(s.get("required", True)),
                skipped=bool(s.get("skipped", False)),
            )
            for s in data.get("steps", [])
        ]
        return cls(steps=steps)


# ast-grep-ignore: ac-django-no-complexity-suppressions
def run_step(  # noqa: PLR0913
    name: str,
    cmd: list[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: int | None = 300,
) -> StepResult:
    """Execute a subprocess command and return a structured result.

    Unlike raw ``subprocess.run(..., check=False)``, this always captures
    output and reports duration, making failures diagnosable.

    Routed through :func:`teatree.core.provision_timebox.run_timeboxed_step`
    so a timeout, or a non-zero exit whose output shows a forked migration
    graph, fires a loud out-of-band user alert and names the diagnosed cause —
    a long provisioning step that cannot complete must alert, never hang
    silently (souliane/teatree#2220). With ``check=False`` a non-zero exit
    stays benign (the historical contract), while a timeout / command-not-found
    is still surfaced as a failure.

    The time-box enhancement is *optional*. ``worktree teardown`` runs the
    worktree's OWN checkout (``uv --directory <worktree> run``), whose base may
    predate ``provision_timebox`` (#2220); the lazy import then raised
    ``ModuleNotFoundError`` and aborted the whole teardown, skipping the steps
    ordered after it — orphaning the DB, the ``Worktree`` row, and containers
    (souliane/teatree#2664). When the module is absent, degrade to a plain
    time-box-free subprocess run that keeps the same ``StepResult`` contract,
    never abort the caller.
    """
    result = _timeboxed_step(name, cmd, cwd=cwd, env=env, timeout=timeout)
    if result.success or check:
        return result
    if result.error.startswith(("timed out", "command not found")):
        return result
    return StepResult(
        name=result.name,
        success=True,
        duration=result.duration,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _timeboxed_step(
    name: str,
    cmd: list[str],
    *,
    cwd: str | Path | None,
    env: dict[str, str] | None,
    timeout: int | None,
) -> StepResult:
    """Run via the time-box enhancement, falling back to a plain subprocess.

    The enhancement (timeout ceiling + heartbeat + forked-migration alert) is
    layered on plain subprocess execution; when the ``provision_timebox`` module
    itself is absent on a stale base it is simply not there, so the plain run is
    the correct degradation (souliane/teatree#2664).

    The catch is narrowed to the module's OWN absence — keyed on
    ``ModuleNotFoundError.name`` — so a *present* ``provision_timebox`` that
    fails to import because of a real internal/transitive-import bug (its
    ``.name`` is the missing DEPENDENCY, not this module) re-raises and surfaces
    via the normal failure path, instead of silently disabling the time-box for
    every healthy install.
    """
    try:
        from teatree.core.provision_timebox import run_timeboxed_step  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        if exc.name != _PROVISION_TIMEBOX_MODULE:
            raise
        logger.warning("provision_timebox unavailable for step %r — plain subprocess run", name)
        return _plain_subprocess_step(name, cmd, cwd=cwd, env=env, timeout=timeout)
    return run_timeboxed_step(name, cmd, cwd=cwd, env=env, timeout=timeout)


def _plain_subprocess_step(
    name: str,
    cmd: list[str],
    *,
    cwd: str | Path | None,
    env: dict[str, str] | None,
    timeout: int | None,
) -> StepResult:
    """Time-box-free subprocess run with the same :class:`StepResult` contract.

    Mirrors :func:`teatree.core.provision_timebox.run_timeboxed_step`'s outcomes
    minus the heartbeat / migration alert: a timeout surfaces a ``"timed out"``
    error, a missing binary a ``"command not found"`` error, and a non-zero exit
    a captured failure — so every ``run_step`` consumer classifies the result
    identically whether or not the enhancement is present. A ``None`` timeout
    resolves to :data:`_PLAIN_STEP_TIMEOUT_FALLBACK_SECONDS` (never an unbounded
    ``subprocess.run``) so the "never hang" invariant holds on this path too.
    """
    ceiling = timeout if timeout is not None else _PLAIN_STEP_TIMEOUT_FALLBACK_SECONDS
    start = time.monotonic()
    try:
        proc = run_allowed_to_fail(cmd, cwd=cwd, env=env, expected_codes=None, timeout=ceiling)
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        return StepResult(name=name, success=False, duration=duration, error=f"timed out after {ceiling}s")
    except OSError as exc:
        duration = time.monotonic() - start
        target = getattr(exc, "filename", None) or (cmd[0] if cmd else str(exc))
        return StepResult(name=name, success=False, duration=duration, error=f"command not found: {target}")
    duration = time.monotonic() - start
    if proc.returncode != 0:
        error = proc.stderr.strip()[:500] if proc.stderr else f"exit code {proc.returncode}"
        return StepResult(
            name=name, success=False, duration=duration, stdout=proc.stdout, stderr=proc.stderr, error=error
        )
    return StepResult(name=name, success=True, duration=duration, stdout=proc.stdout, stderr=proc.stderr)


def run_callable_step(name: str, fn: Callable[[], object]) -> StepResult:
    """Execute a Python callable and return a structured result.

    Wraps arbitrary callables with timing and error capture.
    """
    start = time.monotonic()
    try:
        result = fn()
        duration = time.monotonic() - start
        if isinstance(result, subprocess.CompletedProcess):
            stdout = result.stdout if isinstance(result.stdout, str) else ""
            stderr = result.stderr if isinstance(result.stderr, str) else ""
            if result.returncode != 0:
                error = stderr.strip()[:500] if stderr else f"exit code {result.returncode}"
                return StepResult(
                    name=name,
                    success=False,
                    duration=duration,
                    stdout=stdout,
                    stderr=stderr,
                    error=error,
                )
            return StepResult(name=name, success=True, duration=duration, stdout=stdout, stderr=stderr)
        return StepResult(name=name, success=True, duration=duration)
    except Exception as exc:  # noqa: BLE001
        duration = time.monotonic() - start
        error = str(exc)[:500]
        logger.warning("Step %r raised: %s", name, error)
        return StepResult(name=name, success=False, duration=duration, error=error)


def _timeboxed_subprocess_callable_step(
    name: str, fn: Callable[[], object], *, heavy: bool = False, timeout: float | None = None
) -> StepResult:
    """Run a ``subprocess_only`` provision step wall-clock-bounded, degrading plain.

    The callable sibling of :func:`_timeboxed_step`, for a step the overlay
    affirmed is a pure subprocess shellout touching no ORM (``uv sync``, ``uv
    pip install -e``). Without a wall-clock bound, a child blocked on its PIPE —
    a network stall — hangs the whole provision (souliane/teatree#2244); the
    time-box aborts loud with the named step instead. When ``provision_timebox``
    itself is absent on a stale base (souliane/teatree#2664) this degrades to a
    plain :func:`run_callable_step`, never aborting the caller. The catch is
    narrowed to the module's OWN absence (``ModuleNotFoundError.name``) so a
    present-but-internally-broken module re-raises rather than silently disabling
    the time-box.

    ORM-touching steps (``subprocess_only=False``, the default) never reach
    here — :func:`run_provision_steps` runs them in-process, because Django DB
    connections are per-thread and a worker-thread time-box would write on a
    connection invisible to the caller.

    ``heavy`` selects the ceiling (souliane/teatree#2949):
    :func:`teatree.core.provision_timebox.resolve_step_timeout_seconds` — a
    fast step (the default) aborts within seconds of the short ceiling; a
    heavy step (a DB import, a frontend build) keeps the long one. A caller
    that pre-resolved the ceiling (the parallel path, to keep pool workers
    ORM-free) passes it as *timeout*, bypassing the ``heavy`` lookup here.
    """
    try:
        from teatree.core.provision_timebox import run_timeboxed_callable  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        if exc.name != _PROVISION_TIMEBOX_MODULE:
            raise
        logger.warning("provision_timebox unavailable for subprocess step %r — plain run", name)
        return run_callable_step(name, fn)
    return run_timeboxed_callable(name, fn, heavy=heavy, timeout=timeout)


def _run_single_step(step, *, write: Callable[[str], object], timeout: float | None = None) -> StepResult:  # noqa: ANN001
    """Run one ``ProvisionStep``, honouring its skip-probe first (souliane/teatree#2949).

    A cheap ``skip_probe`` that returns ``True`` means the precondition is
    already satisfied — the (expensive) callable never runs and the step
    records a successful, near-zero :class:`StepResult`. A probe that raises
    is treated as "cannot tell" (never skip) rather than aborting the
    provision — a broken probe must not itself become the failure mode.

    *timeout* is the pre-resolved time-box ceiling. The parallel path resolves it
    on the caller thread (:func:`_run_group_concurrently`) so the pool worker never
    reads the ``ConfigSetting`` store; the serial path passes ``None`` and lets the
    time-box resolve its own ceiling on this (caller) thread.
    """
    if step.skip_probe is not None:
        try:
            should_skip = step.skip_probe()
        except Exception as exc:  # noqa: BLE001 — a broken probe must not abort the provision
            logger.warning("skip_probe for step %r raised: %s — running the step normally", step.name, exc)
            should_skip = False
        if should_skip:
            write(f"  Skipped: {step.name} (precondition already satisfied)")
            return StepResult(name=step.name, success=True, duration=0.0, required=step.required, skipped=True)

    write(f"  Running: {step.name}")
    # #2244: a subprocess-only step (uv sync / uv pip install) shells out
    # with no wall-clock bound, so a child blocked on its PIPE (a network
    # stall) hangs the whole provision — time-box it on a worker thread,
    # which is safe BECAUSE it touches no ORM. An ORM-touching step
    # (subprocess_only=False, the default) runs IN-PROCESS: Django
    # connections are per-thread, so a worker-thread time-box would write on
    # a connection invisible to the caller ("database table is locked" under
    # a test transaction). The db_import callable is the other #2244 hang and
    # is time-boxed separately in worktree_provision via run_timeboxed_db_import.
    if step.subprocess_only:
        result = _timeboxed_subprocess_callable_step(step.name, step.callable, heavy=step.heavy, timeout=timeout)
    else:
        result = run_callable_step(step.name, step.callable)
    success, error = _apply_post_condition(step, result, write=write)
    return StepResult(
        name=result.name,
        success=success,
        duration=result.duration,
        stdout=result.stdout,
        stderr=result.stderr,
        error=error,
        required=step.required,
    )


def _apply_post_condition(
    step: "ProvisionStep", result: StepResult, *, write: Callable[[str], object]
) -> tuple[bool, str]:
    """Fold a step's ``post_condition`` into its success (PR-27).

    A callable that succeeded but whose ``post_condition`` does not hold is
    recorded FAILED — it "ran" without producing its resource. A raising
    post-condition is treated as "not satisfied" (a failure signal, never a
    crash); an already-failed callable is left untouched.
    """
    if not result.success or step.post_condition is None:
        return result.success, result.error
    try:
        held = step.post_condition()
    except Exception as exc:  # noqa: BLE001 — a raising post-condition is a failure, not a crash
        logger.warning("post_condition for step %r raised: %s", step.name, exc)
        write(f"  Post-condition failed: {step.name} ({type(exc).__name__}: {str(exc)[:120]})")
        return False, f"post-condition raised: {type(exc).__name__}: {str(exc)[:200]}"
    if not held:
        write(f"  Post-condition not satisfied: {step.name}")
        return False, "post-condition not satisfied"
    return True, result.error


def _run_group_concurrently(group: list, *, write: Callable[[str], object]) -> list[StepResult]:
    """Run every step in *group* concurrently on a bounded thread pool.

    Only ``subprocess_only`` steps reach here (souliane/teatree#2949, PR-27) —
    each already runs time-boxed on its own worker thread, so several concurrently
    is the same thread-safety contract, just parallel. Membership is decided by
    the dependency DAG (no path between the steps), not a group name; results
    come back in *group* order regardless of completion order.
    """
    names = ", ".join(s.name for s in group)
    write(f"  Running concurrently: {names}")
    # Resolve each member's time-box ceiling HERE, on the caller thread: a pool
    # worker must touch NO ORM. ``resolve_step_timeout_seconds`` reads the
    # ``ConfigSetting`` store, and a Django connection opened on a pool thread is
    # never closed under a Django ``TestCase`` (its atomic wrapping vetoes the
    # ``close()``), so it leaks a ``sqlite3`` ``ResourceWarning`` at GC time.
    # Hoisting the read keeps the workers ORM-free, exactly as _run_single_step's
    # "subprocess_only steps touch no ORM" contract promises.
    timeouts = [_resolve_step_timeout(step) for step in group]
    with ThreadPoolExecutor(max_workers=len(group)) as pool:
        return list(
            pool.map(lambda step, timeout: _run_single_step(step, write=write, timeout=timeout), group, timeouts)
        )


def _resolve_step_timeout(step) -> float | None:  # noqa: ANN001
    """The time-box ceiling for *step*, resolved on the CALLER thread.

    :func:`teatree.core.provision_timebox.resolve_step_timeout_seconds` reads the
    ``ConfigSetting`` store — an ORM access that must not run on a pool worker
    thread (see :func:`_run_group_concurrently`). Degrades to ``None`` — letting
    the time-box fall back to its own plain path — when ``provision_timebox`` is
    absent on a stale base (souliane/teatree#2664).
    """
    try:
        from teatree.core.provision_timebox import resolve_step_timeout_seconds  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        if exc.name != _PROVISION_TIMEBOX_MODULE:
            raise
        return None
    return float(resolve_step_timeout_seconds(heavy=step.heavy))


# ast-grep-ignore: ac-django-no-complexity-suppressions
def _report_one_result(  # noqa: PLR0913 — each kwarg is a documented output sink, not poor design.
    member,  # noqa: ANN001
    result: StepResult,
    *,
    verbose: bool,
    write: Callable[[str], object],
    write_err: Callable[[str], object],
    stop_on_required_failure: bool,
) -> bool:
    """Log one step's outcome; return whether it should halt the whole run."""
    if result.skipped:
        return False
    if verbose and result.stdout:
        for line in result.stdout.strip().splitlines()[:20]:
            write(f"    | {line}")
    if result.success:
        if verbose:
            write(f"    OK ({result.duration:.1f}s)")
        return False
    write_err(result.summary())
    if verbose and result.stderr:
        for line in result.stderr.strip().splitlines()[:20]:
            write_err(f"    | {line}")
    _alert_on_migration_conflict(result)
    if member.required and stop_on_required_failure:
        write_err(f"  HALTED: required step '{member.name}' failed.")
        return True
    return False


def _resolve_step_deps(steps: list) -> dict[str, set[str]]:
    """Map each step name to the set of step names it must run AFTER (PR-27 DAG).

    Edges come from ``requires``/``produces``: a step depends on every step that
    produces a token it requires. A required token no step produces raises loud
    (fail-closed) — never a silently-skipped declared dependency.
    """
    producers: dict[str, set[str]] = {}
    for step in steps:
        for token in step.produces:
            producers.setdefault(token, set()).add(step.name)
    deps: dict[str, set[str]] = {}
    for step in steps:
        step_deps: set[str] = set()
        for token in step.requires:
            if token not in producers:
                msg = f"provision step {step.name!r} requires {token!r} which no step produces"
                raise ValueError(msg)
            step_deps |= producers[token] - {step.name}
        deps[step.name] = step_deps
    return deps


def _run_subprocess_wave(subprocess_ready: list, *, write: Callable[[str], object]) -> list[StepResult]:
    """Run a wave's independent, ORM-free ``subprocess_only`` steps, concurrently when >1.

    A bounded pool is the same thread-safety contract as one worker; results come
    back in *subprocess_ready* order.
    """
    if len(subprocess_ready) > 1:
        return _run_group_concurrently(subprocess_ready, write=write)
    return [_run_single_step(subprocess_ready[0], write=write)]


def run_provision_steps(
    steps: list,
    *,
    verbose: bool = False,
    stdout_writer: Callable[[str], object] | None = None,
    stderr_writer: Callable[[str], object] | None = None,
    stop_on_required_failure: bool = True,
) -> ProvisionReport:
    """Execute ProvisionStep objects in dependency order and collect results (PR-27).

    Steps run in a topological schedule from their ``requires``/``produces``
    edges; steps with no dependency path between them run in one wave — the
    ``subprocess_only`` ones concurrently on a bounded pool, ORM steps serially
    in-process — and each step's ``post_condition`` is folded into its success.
    With *stop_on_required_failure* True (default) the run halts after a wave in
    which a required step failed, so downstream steps never start. A ``requires``
    token no step produces, or a cycle, raises ``ValueError`` (fail-closed).
    """
    report = ProvisionReport()
    write = stdout_writer or (lambda _msg: None)
    write_err = stderr_writer or (lambda _msg: None)

    deps = _resolve_step_deps(steps)
    completed: set[str] = set()
    pending = list(steps)

    def _record(step: "ProvisionStep", result: StepResult) -> bool:
        completed.add(step.name)
        report.steps.append(result)
        return _report_one_result(
            step,
            result,
            verbose=verbose,
            write=write,
            write_err=write_err,
            stop_on_required_failure=stop_on_required_failure,
        )

    while pending:
        ready = [step for step in pending if deps[step.name] <= completed]
        if not ready:
            blocked = [step.name for step in pending]
            msg = f"provision steps have an unsatisfiable dependency cycle among {blocked}"
            raise ValueError(msg)

        halt = False
        # Concurrent subprocess batch first (already launched, so its whole
        # result set is reported), then ORM steps serially with fail-fast BETWEEN
        # them — so the default no-edge case keeps the old sequential halt
        # semantics while independent subprocess steps still run concurrently.
        subprocess_ready = [step for step in ready if step.subprocess_only]
        if subprocess_ready:
            for step, result in zip(subprocess_ready, _run_subprocess_wave(subprocess_ready, write=write), strict=True):
                halt = _record(step, result) or halt
        for step in ready:
            if step.subprocess_only:
                continue
            if halt:
                break
            halt = _record(step, _run_single_step(step, write=write)) or halt

        pending = [step for step in pending if step.name not in completed]
        if halt:
            break

    return report


def _alert_on_migration_conflict(result: StepResult) -> None:
    """Fire a loud user alert when a failed step's output shows a forked graph.

    Covers callable-based steps (overlay ``migrate`` / ``db_import`` that
    return a ``CompletedProcess``) whose output never flows through
    :func:`run_step`'s subprocess time-box. A forked migration graph hit by
    such a step is diagnosed by its symptom — "rebase/renumber needed" — and
    surfaced out-of-band, instead of leaving the agent to discover the grind
    (souliane/teatree#2220). Best-effort: the alert never breaks provisioning —
    including when the executing base predates ``provision_timebox`` itself
    (souliane/teatree#2664), in which case there is no alert path and the call is
    a no-op. The catch is narrowed to the module's OWN absence (keyed on
    ``ModuleNotFoundError.name``) so a *present* module with a real internal
    import bug re-raises rather than silently suppressing the alert.
    """
    try:
        from teatree.core.provision_timebox import alert_provision_user, detect_migration_conflict  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        if exc.name != _PROVISION_TIMEBOX_MODULE:
            raise
        return

    conflict = detect_migration_conflict(f"{result.stdout}\n{result.stderr}\n{result.error}")
    if conflict is not None:
        logger.warning("Provisioning step %r hit a %s", result.name, conflict)
        alert_provision_user(step=result.name, repo="", detail=conflict)
