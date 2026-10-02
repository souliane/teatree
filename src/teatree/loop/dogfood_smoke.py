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
A step also carries its own env overlay, which is how the sequence
reproduces the multi-overlay resolution failure fixed earlier: the
workspace verbs run once with :data:`OVERLAY_ENV_VAR` removed and once
with it pinned, so a regression in either route is categorised on its own.

Wiring layers (the management command, the loop scanner) compose this
runner; they never re-implement the orchestration shape.
"""

import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase

#: The env var ``get_overlay()`` reads first; unsetting it is this failure mode.
OVERLAY_ENV_VAR = "T3_OVERLAY_NAME"

#: Stands in for the worktree ``workspace_ticket`` creates. :func:`default_steps` is a
#: static list built before any step runs, so no step can name that path yet — the
#: placeholder is bound by :func:`run_smoke` once the creating step has succeeded.
WORKTREE_PATH_PLACEHOLDER = "<worktree-path>"

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
    OVERLAY_RESOLUTION_FAILED = "overlay_resolution_failed"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


#: Step name → outcome kind to emit if the step fails. Kept here so the
#: management command and scanner share the canonical mapping.
STEP_OUTCOME_KIND: dict[str, SmokeOutcomeKind] = {
    "workspace_ticket": SmokeOutcomeKind.PROVISION_FAILED,
    "env_show": SmokeOutcomeKind.PROVISION_FAILED,
    "worktree_provision": SmokeOutcomeKind.PROVISION_FAILED,
    "workspace_provision_env_unset": SmokeOutcomeKind.OVERLAY_RESOLUTION_FAILED,
    "workspace_provision_env_set": SmokeOutcomeKind.PROVISION_FAILED,
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
    #: Vars removed from this step's child env — how the smoke reproduces the
    #: bare-``get_overlay()`` failure mode fixed earlier (see :data:`OVERLAY_ENV_VAR`).
    env_unset: frozenset[str] = frozenset()
    env_overrides: Mapping[str, str] = field(default_factory=dict)


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
    #: Acceptance items this run could NOT exercise — surfaced in the summary so
    #: a green run never reads as proof of coverage it does not have.
    uncovered: list[str] = field(default_factory=list)

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

#: Answers "where is the worktree ``workspace_ticket`` created", or '' when none
#: materialised. Injected so this module stays free of the ORM the answer comes from.
type WorktreePathResolver = Callable[[], str]


class _WorktreePathBinder:
    """Bind :data:`WORKTREE_PATH_PLACEHOLDER` to the created worktree, memoised per run.

    Resolution is deferred to the first step that needs the path — before
    ``workspace_ticket`` has run there is no worktree to name — and memoised
    afterwards, so no two steps of one sequence can target different paths.
    """

    def __init__(self, resolver: WorktreePathResolver | None) -> None:
        self._resolver = resolver
        self._path = ""
        self._failure = ""
        self._resolved = False

    def bind(self, step: SmokeStep) -> tuple[SmokeStep, str]:
        """Return the path-bound step plus a failure reason ('' when bound)."""
        if WORKTREE_PATH_PLACEHOLDER not in step.command:
            return step, ""
        self._resolve_once()
        if self._failure:
            return step, self._failure
        command = tuple(self._path if part == WORKTREE_PATH_PLACEHOLDER else part for part in step.command)
        return replace(step, command=command), ""

    def _resolve_once(self) -> None:
        if self._resolved:
            return
        self._resolved = True
        if self._resolver is None:
            self._failure = "no worktree-path resolver was injected"
            return
        try:
            self._path = self._resolver()
        except Exception as exc:
            logger.exception("Worktree-path resolver crashed")
            self._failure = f"{type(exc).__name__}: {exc}"
            return
        if not self._path:
            self._failure = "no materialised worktree recorded for the fixture ticket"


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


def _step_env(step: SmokeStep) -> dict[str, str]:
    """The step's child env — the cleaned parent env, minus ``env_unset``, plus overrides."""
    env = _clean_subprocess_env()
    for key in step.env_unset:
        env.pop(key, None)
    env.update(step.env_overrides)
    return env


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
            env=_step_env(step),
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

    Every step but the creating one and the workspace-wide sweep targets the
    new worktree explicitly by ``--path``, since none of them runs with that
    worktree as its CWD; the path is :data:`WORKTREE_PATH_PLACEHOLDER` here and
    is bound by :func:`run_smoke`.
    """
    ticket_command: tuple[str, ...] = ("t3", overlay, "workspace", "ticket", fixture_ticket_url)
    if variant:
        ticket_command = (*ticket_command, "--variant", variant)
    unset_overlay = frozenset({OVERLAY_ENV_VAR})
    at_worktree = ("--path", WORKTREE_PATH_PLACEHOLDER)
    return [
        SmokeStep(name="workspace_ticket", command=ticket_command),
        SmokeStep(name="env_show", command=("t3", overlay, "env", "show", *at_worktree)),
        SmokeStep(name="worktree_provision", command=("t3", overlay, "worktree", "provision", *at_worktree)),
        SmokeStep(
            name="workspace_provision_env_unset",
            command=("t3", overlay, "workspace", "provision", *at_worktree),
            env_unset=unset_overlay,
        ),
        SmokeStep(
            name="workspace_provision_env_set",
            command=("t3", overlay, "workspace", "provision", *at_worktree),
            env_overrides={OVERLAY_ENV_VAR: overlay},
        ),
        # start/ready run with the var unset too — the acceptance asks for the
        # existing steps under the failure-mode env, not for duplicated steps.
        SmokeStep(
            name="worktree_start",
            command=("t3", overlay, "worktree", "start", *at_worktree),
            timeout_seconds=120,
            env_unset=unset_overlay,
        ),
        SmokeStep(
            name="worktree_ready",
            command=("t3", overlay, "worktree", "ready", *at_worktree),
            timeout_seconds=120,
            env_unset=unset_overlay,
        ),
        SmokeStep(name="worktree_teardown", command=("t3", overlay, "worktree", "teardown", *at_worktree)),
        SmokeStep(name="workspace_clean_all", command=("t3", overlay, "workspace", "clean-all")),
    ]


def total_step_budget_seconds(steps: Sequence[SmokeStep]) -> int:
    """Worst-case wall-clock of the sequence — every step running to its own ceiling.

    The smoke reaches the user as ONE command, so this total must stay under
    whatever per-command ceiling the caller runs it beneath; a kill from the
    outer ceiling destroys the categorised verdict and the failure DM that are
    the smoke's whole output.
    """
    return sum(step.timeout_seconds for step in steps)


def pick_alias_variant(overlay: "OverlayBase") -> str:
    """Return a known variant whose canonical tenant differs from its own name.

    An identity-mapped variant never exercises the variant→tenant alias path,
    so a smoke run against one passes while a non-identity alias bug ships
    (#1308 acceptance). Picks from the LEFT side of the overlay's alias map;
    empty when the overlay declares no such variant.
    """
    for name in overlay.config.known_variants:
        candidate = name.strip()
        if not candidate:
            continue
        try:
            resolved = overlay.provisioning.resolve_variant(candidate)
        except Exception:
            logger.exception("Overlay failed to resolve variant %s — treating it as uncovered", candidate)
            continue
        if resolved.canonical_tenant != candidate:
            return candidate
    return ""


def run_smoke(
    steps: Sequence[SmokeStep],
    *,
    runner: StepRunner = run_t3_command,
    uncovered: Sequence[str] = (),
    resolve_worktree_path: WorktreePathResolver | None = None,
) -> SmokeReport:
    """Execute the smoke sequence and produce a categorised :class:`SmokeReport`.

    Stops on the first failing step (or timeout) — a green teardown
    cannot prove the rest of the sequence, and a broken provision step
    invalidates everything that follows. The verdict mapping comes from
    :data:`STEP_OUTCOME_KIND`; an unmapped step name degrades to
    :attr:`SmokeOutcomeKind.UNKNOWN` so a future step the table forgets
    still produces a failure (vs. silently passing).

    ``resolve_worktree_path`` answers where ``workspace_ticket`` put the
    worktree. A step needing it that cannot get it FAILS here and is
    categorised: running it pathless falls back to CWD auto-detection, which
    reports the operator's shell as the culprit for a smoke defect.
    """
    report = SmokeReport(uncovered=list(uncovered))
    binder = _WorktreePathBinder(resolve_worktree_path)
    for step in steps:
        runnable, unresolved = binder.bind(step)
        if unresolved:
            report.steps.append(
                StepResult(
                    step=step,
                    returncode=-3,
                    stderr=f"worktree path unresolved: {unresolved}",
                    stdout="",
                    elapsed_seconds=0.0,
                ),
            )
            report.outcome = STEP_OUTCOME_KIND.get(step.name, SmokeOutcomeKind.UNKNOWN)
            report.failing_step = step.name
            return report
        try:
            result = runner(runnable)
        except Exception as exc:
            logger.exception("Smoke runner crashed on step %s", step.name)
            result = StepResult(
                step=runnable,
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
    suffix = f"; uncovered: {', '.join(report.uncovered)}" if report.uncovered else ""
    if report.passed:
        steps = len(report.steps)
        return f"dogfood smoke PASS ({steps} steps){suffix}"
    stderr_tail = report.failing_step_stderr.strip().splitlines()[-1:] if report.failing_step_stderr else []
    tail = stderr_tail[0] if stderr_tail else ""
    if tail:
        return f"dogfood smoke {report.outcome.value} at {report.failing_step}: {tail[:120]}{suffix}"
    return f"dogfood smoke {report.outcome.value} at {report.failing_step}{suffix}"


__all__ = [
    "OVERLAY_ENV_VAR",
    "STEP_OUTCOME_KIND",
    "WORKTREE_PATH_PLACEHOLDER",
    "SmokeOutcomeKind",
    "SmokeReport",
    "SmokeStep",
    "StepResult",
    "StepRunner",
    "WorktreePathResolver",
    "default_steps",
    "pick_alias_variant",
    "report_summary",
    "run_smoke",
    "run_t3_command",
    "total_step_budget_seconds",
]
