"""Structured step execution with error propagation and reporting.

Replaces the fire-and-forget ``partial(subprocess.run, ..., check=False)``
pattern with explicit success/failure tracking per step.
"""

import logging
import subprocess  # noqa: S404
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
        return all(s.success for s in self.steps)

    @property
    def failed_step(self) -> str | None:
        for s in self.steps:
            if not s.success:
                return s.name
        return None

    @property
    def failed_required_step(self) -> str | None:
        """Return the name of the first failed required step, if any.

        This depends on the ``required`` flag from the original ProvisionStep.
        Since StepResult doesn't carry ``required``, callers track this
        externally when needed (see ``run_provision_steps``).
        """
        return self.failed_step

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
    """
    start = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        duration = time.monotonic() - start
        if proc.returncode != 0 and check:
            error = proc.stderr.strip()[:500] if proc.stderr else f"exit code {proc.returncode}"
            logger.warning("Step %r failed: %s", name, error)
            return StepResult(
                name=name,
                success=False,
                duration=duration,
                stdout=proc.stdout,
                stderr=proc.stderr,
                error=error,
            )
        return StepResult(
            name=name,
            success=True,
            duration=duration,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        error = f"timed out after {timeout}s"
        logger.warning("Step %r %s", name, error)
        return StepResult(name=name, success=False, duration=duration, error=error)
    except OSError as exc:
        duration = time.monotonic() - start
        error = f"command not found: {getattr(exc, 'filename', cmd[0]) if cmd else exc}"
        logger.warning("Step %r: %s", name, error)
        return StepResult(name=name, success=False, duration=duration, error=error)


def run_callable_step(name: str, fn: Callable[[], object]) -> StepResult:
    """Execute a Python callable and return a structured result.

    Wraps arbitrary callables (including legacy ``partial(subprocess.run, ...)``
    patterns) with timing and error capture.
    """
    start = time.monotonic()
    try:
        result = fn()
        duration = time.monotonic() - start
        # Handle legacy subprocess.run return values
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
        report.steps.append(result)

        if verbose and result.stdout:
            for line in result.stdout.strip().splitlines()[:20]:
                write(f"    | {line}")
        if not result.success:
            write_err(result.summary())
            if verbose and result.stderr:
                for line in result.stderr.strip().splitlines()[:20]:
                    write_err(f"    | {line}")
            if step.required and stop_on_required_failure:
                write_err(f"  HALTED: required step '{step.name}' failed.")
                break
        elif verbose:
            write(f"    OK ({result.duration:.1f}s)")

    return report
