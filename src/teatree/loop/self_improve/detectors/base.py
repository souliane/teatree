"""Base ``SelfImproveDetector`` protocol + ``DetectorReport`` value object.

A self-improve detector is a ``Scanner`` (it returns ``ScanSignal`` rows
for the rendering pass) plus a richer ``DetectorReport`` carrying the
dedup contract: ``dedup_key`` / ``state_hash`` / ladder ceiling /
``auto_fix`` flag.  The schedule module reads the reports and decides
whether a firing fires fresh, dedups against an existing row, or
escalates one rung.
"""

from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, runtime_checkable

from teatree.core.models.self_improve_firing import SelfImproveFiring
from teatree.loop.scanners.base import ScanSignal

__all__ = ["ActionRung", "DetectorReport", "SelfImproveDetector", "fresh_or_escalated"]


class ActionRung:
    """String constants for ladder rungs (mirror ``SelfImproveFiring.Action``).

    Defined here (not in ``actions.py``) so detectors can declare their
    ``max_rung`` without importing the action ladder — keeps the
    detectors -> actions dependency one-directional.  The values are
    literal strings (not ``.value`` lookups on ``TextChoices``) because
    some static type-checkers narrow ``TextChoices.X.value`` to a
    ``tuple[Literal, Literal]`` and lose the ``str`` invariant the rest
    of the module needs.  ``test_ladder_constants_match_model_choices``
    asserts the strings stay aligned with the model choices.
    """

    LOG: str = "log"
    STATUSLINE: str = "statusline"
    SLACK: str = "slack"
    TICKET: str = "ticket"
    AUTO_FIX: str = "auto_fix"


# Drift guard: `test_ladder_constants_match_model_choices` asserts every
# ActionRung value is a SelfImproveFiring.Action choice, so a future rung
# rename cannot diverge silently from the DB choice value.
_RUNG_CHOICES = {
    ActionRung.LOG: SelfImproveFiring.Action.LOG,
    ActionRung.STATUSLINE: SelfImproveFiring.Action.STATUSLINE,
    ActionRung.SLACK: SelfImproveFiring.Action.SLACK,
    ActionRung.TICKET: SelfImproveFiring.Action.TICKET,
    ActionRung.AUTO_FIX: SelfImproveFiring.Action.AUTO_FIX,
}


@dataclass(frozen=True, slots=True)
class DetectorReport:
    """One detector observation, fully self-described.

    ``dedup_key`` is the canonical identity (see
    :func:`teatree.loop.self_improve.dedup.canonical_key`); same key +
    same ``state_hash`` is suppressed by cool-down.  ``max_rung`` caps
    the ladder so a detector cannot escalate past its declared ceiling.
    """

    detector: str
    dedup_key: str
    state_hash: str
    severity: str
    max_rung: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    auto_fix: bool = False

    def to_signal(self) -> ScanSignal:
        """Emit the ``ScanSignal`` the rendering layer reads.

        ``kind`` is prefixed with ``self_improve.`` so the dispatcher can
        route every self-improve signal through one branch without
        learning the detector inventory.
        """
        return ScanSignal(
            kind=f"self_improve.{self.detector}",
            summary=f"[self-improve] {self.summary}",
            payload={
                "detector": self.detector,
                "dedup_key": self.dedup_key,
                "state_hash": self.state_hash,
                "severity": self.severity,
                "max_rung": self.max_rung,
                "auto_fix": self.auto_fix,
                **self.payload,
            },
        )


@runtime_checkable
class SelfImproveDetector(Protocol):
    """Protocol every Phase 1 detector implements."""

    name: ClassVar[str]
    tier: ClassVar[str]
    severity: ClassVar[str]
    max_rung: ClassVar[str]
    auto_fix: ClassVar[bool]

    def scan(self) -> list[ScanSignal]: ...  # pragma: no branch

    def detect(self) -> list[DetectorReport]: ...  # pragma: no branch


def fresh_or_escalated(report: DetectorReport, firing: SelfImproveFiring | None) -> bool:
    """Return ``True`` iff this report should advance the action ladder.

    ``None`` firing ⇒ first observation, advance.  Same ``state_hash``
    ⇒ within cool-down, hold.  Different ``state_hash`` ⇒ evidence
    changed, advance one rung.
    """
    if firing is None:
        return True
    return firing.state_hash != report.state_hash
