"""Overlay provision-smoke harness — exercise an overlay's provision path end-to-end (#1308).

The loop reaches for an overlay's provision path only when the user
needs E2E, so latent CLI bugs accumulate quietly between runs and
surface as a cascade at the worst possible time (mid-E2E session).
This module owns the testable smoke runner that exercises the
canonical provision path against a fixture ticket so bugs surface in
the loop's tick, not in the user's next session.

The runner is pure orchestration over an injectable :class:`SmokeStep`
list — production wiring runs ``t3 <overlay> workspace ticket /
worktree provision / start / ready / teardown / workspace clean-all``
and friends; tests inject in-memory fakes. Each step has a per-step
time budget (default 60s); exceeding it categorises the run as
:attr:`SmokeOutcomeKind.TIMEOUT` so the failure DM names the hung step.

Wiring layers (the management command, the loop scanner) compose this
runner; they never re-implement the orchestration shape.
"""

import logging
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

logger = logging.getLogger(__name__)


class SmokeOutcomeKind(StrEnum):
    """Categorised verdicts the harness can emit.

    The categories mirror the issue's "failing step" semantics — the DM
    body and statusline summary key off the verdict so the user
    instantly knows whether provision/start/ready/teardown is broken
    without reading the traceback.
    """

    PASS = "pass"  # noqa: S105 — outcome kind, not a credential
    PROVISION_FAILED = "provision_failed"
    START_FAILED = "start_failed"
    READY_FAILED = "ready_failed"
    TEARDOWN_FAILED = "teardown_failed"
    CLEAN_FAILED = "clean_failed"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


#: Step name → outcome kind to emit if the step fails. Kept here so the
#: management command and scanner share the canonical mapping.
STEP_OUTCOME_KIND: dict[str, SmokeOutcomeKind] = {
    "workspace_ticket": SmokeOutcomeKind.PROVISION_FAILED,
    "env_show": SmokeOutcomeKind.PROVISION_FAILED,
    "worktree_provision": SmokeOutcomeKind.PROVISION_FAILED,
    "worktree_start": SmokeOutcomeKind.START_FAILED,
    "worktree_ready": SmokeOutcomeKind.READY_FAILED,
    "worktree_teardown": SmokeOutcomeKind.TEARDOWN_FAILED,
    "workspace_clean_all": SmokeOutcomeKind.CLEAN_FAILED,
}


@dataclass(frozen=True, slots=True)
class SmokeStep:
    """One executable step in the smoke sequence.

    ``runner`` is the callable that performs the step. The default is
    :func:`run_t3_command`, which shells out to ``t3 ...``. Tests inject
    in-memory fakes; the scanner uses a dry-run runner to avoid the
    minutes-long live execution.
    """

    name: str
    command: tuple[str, ...]
    timeout_seconds: int = 60


@dataclass(frozen=True, slots=True)
class StepResult:
    """Per-step outcome — exit code, captured stderr, elapsed seconds."""

    step: SmokeStep
    returncode: int
    stderr: str
    stdout: str
    elapsed_seconds: float
    timed_out: bool = False


@dataclass(slots=True)
class SmokeReport:
    """Aggregate verdict + per-step trail of a single smoke run.

    Consumers (CLI exit code, DM body, scanner signal) read ``outcome``
    and ``failing_step``; the trail of :class:`StepResult` rows is the
    evidence for debugging or attaching to the DM.
    """

    outcome: SmokeOutcomeKind = SmokeOutcomeKind.PASS
    failing_step: str = ""
    steps: list[StepResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.outcome is SmokeOutcomeKind.PASS

    @property
    def failing_step_stderr(self) -> str:
        """Captured stderr of the failing step (empty when ``passed``)."""
        for result in self.steps:
            if result.step.name == self.failing_step:
                return result.stderr
        return ""


#: Type of the per-step runner — separated so tests can inject a fake
#: without monkey-patching :mod:`subprocess`.
type StepRunner = Callable[[SmokeStep], StepResult]


def _decode_subprocess_output(raw: bytes | str | None) -> str:
    """Coerce a possibly-bytes subprocess output to text for the report."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def _clean_subprocess_env() -> dict[str, str]:
    """Strip an inherited ``DJANGO_SETTINGS_MODULE`` for the step's ``t3`` child.

    This process has already bootstrapped Django by the time a step runs,
    which leaks ``DJANGO_SETTINGS_MODULE`` into ``os.environ`` (``ensure_django()``'s
    ``setdefault``). A pre-set value crashes the child's overlay-entry-point
    import with ``AppRegistryNotReady`` before it ever reaches its own command
    body — the same class of leak :func:`teatree.cli.overlay._base_env` and
    :func:`teatree.self_update._self_db_migrate_env` strip for their own
    subprocess calls.
    """
    return {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}


def run_t3_command(step: SmokeStep) -> StepResult:
    """Default runner: shell out to the step's CLI command.

    Routes the call through :func:`teatree.utils.run.run_allowed_to_fail`
    with ``expected_codes=None`` so any exit code is captured (the
    orchestrator categorises failures itself). Converts a
    :class:`TimeoutExpired` into a ``timed_out`` :class:`StepResult`
    rather than re-raising — the orchestrator owns the verdict mapping.
    """
    started = time.monotonic()
    try:
        completed = run_allowed_to_fail(
            step.command,
            expected_codes=None,
            timeout=step.timeout_seconds,
            env=_clean_subprocess_env(),
        )
    except TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        return StepResult(
            step=step,
            returncode=-1,
            stderr=_decode_subprocess_output(exc.stderr),
            stdout=_decode_subprocess_output(exc.stdout),
            elapsed_seconds=elapsed,
            timed_out=True,
        )
    return StepResult(
        step=step,
        returncode=completed.returncode,
        stderr=completed.stderr,
        stdout=completed.stdout,
        elapsed_seconds=time.monotonic() - started,
    )


def default_steps(*, overlay: str, fixture_ticket_url: str, variant: str = "") -> list[SmokeStep]:
    """Canonical overlay provision-smoke sequence (#1308 § "Smoke shape").

    Order matters — provision must precede start, start precedes ready,
    ready precedes teardown, teardown precedes the clean-all sweep.
    Steps are pure data; the runner consumes them. The ``overlay`` short
    name (e.g. the value the CLI sees after ``t3 <overlay>``) is required
    so the generated commands target the right overlay sub-app; the
    ``variant`` flag is only emitted when non-empty, since some overlays
    do not segment their tenants by variant.
    """
    ticket_command: tuple[str, ...] = ("t3", overlay, "workspace", "ticket", fixture_ticket_url)
    if variant:
        ticket_command = (*ticket_command, "--variant", variant)
    return [
        SmokeStep(name="workspace_ticket", command=ticket_command),
        SmokeStep(name="env_show", command=("t3", overlay, "env", "show")),
        SmokeStep(name="worktree_provision", command=("t3", overlay, "worktree", "provision")),
        SmokeStep(name="worktree_start", command=("t3", overlay, "worktree", "start"), timeout_seconds=120),
        SmokeStep(name="worktree_ready", command=("t3", overlay, "worktree", "ready"), timeout_seconds=120),
        SmokeStep(name="worktree_teardown", command=("t3", overlay, "worktree", "teardown")),
        SmokeStep(name="workspace_clean_all", command=("t3", overlay, "workspace", "clean-all")),
    ]


def run_smoke(steps: Sequence[SmokeStep], *, runner: StepRunner = run_t3_command) -> SmokeReport:
    """Execute the smoke sequence and produce a categorised :class:`SmokeReport`.

    Stops on the first failing step (or timeout) — a green teardown
    cannot prove the rest of the sequence, and a broken provision step
    invalidates everything that follows. The verdict mapping comes from
    :data:`STEP_OUTCOME_KIND`; an unmapped step name degrades to
    :attr:`SmokeOutcomeKind.UNKNOWN` so a future step the table forgets
    still produces a failure (vs. silently passing).
    """
    report = SmokeReport()
    for step in steps:
        try:
            result = runner(step)
        except Exception as exc:
            logger.exception("Smoke runner crashed on step %s", step.name)
            result = StepResult(
                step=step,
                returncode=-2,
                stderr=f"runner crashed: {type(exc).__name__}: {exc}",
                stdout="",
                elapsed_seconds=0.0,
            )
        report.steps.append(result)
        if result.timed_out:
            report.outcome = SmokeOutcomeKind.TIMEOUT
            report.failing_step = step.name
            return report
        if result.returncode != 0:
            report.outcome = STEP_OUTCOME_KIND.get(step.name, SmokeOutcomeKind.UNKNOWN)
            report.failing_step = step.name
            return report
    return report


def report_summary(report: SmokeReport) -> str:
    """One-line statusline-friendly summary of a smoke run."""
    if report.passed:
        steps = len(report.steps)
        return f"dogfood smoke PASS ({steps} steps)"
    stderr_tail = report.failing_step_stderr.strip().splitlines()[-1:] if report.failing_step_stderr else []
    tail = stderr_tail[0] if stderr_tail else ""
    if tail:
        return f"dogfood smoke {report.outcome.value} at {report.failing_step}: {tail[:120]}"
    return f"dogfood smoke {report.outcome.value} at {report.failing_step}"


__all__ = [
    "STEP_OUTCOME_KIND",
    "SmokeOutcomeKind",
    "SmokeReport",
    "SmokeStep",
    "StepResult",
    "StepRunner",
    "default_steps",
    "report_summary",
    "run_smoke",
    "run_t3_command",
]
