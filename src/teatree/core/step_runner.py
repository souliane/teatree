"""Structured step execution with error propagation and reporting.

Replaces the fire-and-forget ``partial(subprocess.run, ..., check=False)``
pattern with explicit success/failure tracking per step.
"""

import logging
import subprocess  # noqa: S404 — only TimeoutExpired/CompletedProcess accessed, no shelling
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from teatree.utils.run import run_allowed_to_fail

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

    def summary(self) -> str:
        status = "OK" if self.success else "FAILED"
        msg = f"  [{status}] {self.name} ({self.duration:.1f}s)"
        if not self.success and self.error:
            msg += f"\n         {self.error}"
        return msg


@dataclass
class ProvisionReport:
    """Aggregated outcome of a multi-step provisioning run."""

    steps: list[StepResult] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not any(s.required and not s.success for s in self.steps)

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
        lines.append(f"\n  {ok}/{total} steps succeeded.")
        if not self.success:
            lines.append(f"  First failure: {self.failed_step}")
        return "\n".join(lines)


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


def _timeboxed_subprocess_callable_step(name: str, fn: Callable[[], object]) -> StepResult:
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
    """
    try:
        from teatree.core.provision_timebox import run_timeboxed_callable  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        if exc.name != _PROVISION_TIMEBOX_MODULE:
            raise
        logger.warning("provision_timebox unavailable for subprocess step %r — plain run", name)
        return run_callable_step(name, fn)
    return run_timeboxed_callable(name, fn)


def run_provision_steps(
    steps: list,
    *,
    verbose: bool = False,
    stdout_writer: Callable[[str], object] | None = None,
    stderr_writer: Callable[[str], object] | None = None,
    stop_on_required_failure: bool = True,
) -> ProvisionReport:
    """Execute a list of ProvisionStep objects and collect results.

    When *stop_on_required_failure* is True (default), execution halts after
    the first failure of a step with ``required=True``.  Optional steps
    (``required=False``) never halt execution.
    """
    report = ProvisionReport()
    write = stdout_writer or (lambda _msg: None)
    write_err = stderr_writer or (lambda _msg: None)

    for step in steps:
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
            result = _timeboxed_subprocess_callable_step(step.name, step.callable)
        else:
            result = run_callable_step(step.name, step.callable)
        result = StepResult(
            name=result.name,
            success=result.success,
            duration=result.duration,
            stdout=result.stdout,
            stderr=result.stderr,
            error=result.error,
            required=step.required,
        )
        report.steps.append(result)

        if verbose and result.stdout:
            for line in result.stdout.strip().splitlines()[:20]:
                write(f"    | {line}")
        if not result.success:
            write_err(result.summary())
            if verbose and result.stderr:
                for line in result.stderr.strip().splitlines()[:20]:
                    write_err(f"    | {line}")
            _alert_on_migration_conflict(result)
            if step.required and stop_on_required_failure:
                write_err(f"  HALTED: required step '{step.name}' failed.")
                break
        elif verbose:
            write(f"    OK ({result.duration:.1f}s)")

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
