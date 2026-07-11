"""Result value objects for a worktree provisioning run.

The typed outcomes the execution engine in :mod:`teatree.core.provision.step_runner`
produces and reports: a per-step :class:`StepResult`, the aggregated
:class:`ProvisionReport`, their JSON-safe ``…Dict`` projections persisted on
``Worktree.extra``, and the :class:`StepReporter` output-sink bundle the run
writes progress to. Kept separate from the engine so each file stays
single-concern — and so this module depends on nothing in the provision package
(the engine and the time-box both import these types, never the reverse).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypedDict


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


@dataclass(frozen=True, slots=True)
class StepReporter:
    """The output sinks a provisioning run writes its progress and failures to.

    The three knobs that always travel together through the reporting path:
    *write* is the stdout sink (per-step start/skip/OK lines), *write_err* the
    stderr sink (failure summaries, the HALTED line), *verbose* whether to echo a
    step's captured stdout/stderr and per-step OK timings. Bundling them keeps the
    reporting functions from re-threading two writers plus a flag.
    """

    write: Callable[[str], object]
    write_err: Callable[[str], object]
    verbose: bool = False
