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

logger = logging.getLogger(__name__)


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
    """
    from teatree.core.provision_timebox import run_timeboxed_step  # noqa: PLC0415

    result = run_timeboxed_step(name, cmd, cwd=cwd, env=env, timeout=timeout)
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
    (souliane/teatree#2220). Best-effort: the alert never breaks provisioning.
    """
    from teatree.core.provision_timebox import alert_provision_user, detect_migration_conflict  # noqa: PLC0415

    conflict = detect_migration_conflict(f"{result.stdout}\n{result.stderr}\n{result.error}")
    if conflict is not None:
        logger.warning("Provisioning step %r hit a %s", result.name, conflict)
        alert_provision_user(step=result.name, repo="", detail=conflict)
