"""Every gate finding nobody has decided becomes ONE question, exactly once (16A).

The doctor has no hands. Measured: 51 files in ``cli/doctor/``, and not one of them creates a
Ticket, a Task or a Question — every ``DeferredQuestion`` reference there is a read. So twelve
gates were printed at every session start for up to 85 days to a human who had to notice and
act. The factory did not ignore the alarm; the alarm reached nothing.

**The sink is a question, not an issue.** The inert report says so itself — "each line above is
a decision nobody has made" — and a decision is a question. Questions are already surfaced to
the owner and do not touch the factory-authored-ticket cap, which matters: one issue per finding
would turn a 234-issue backlog into a 300-issue one, the opposite of the instruction.

**One question covers the whole undecided SET.** Per-gate questions put eleven of these in the
owner's backlog at once, which is a list to work through rather than a call to make — the whole
set is one decision, taken once. Dedup is not designed here, it is used:
:attr:`~teatree.core.models.deferred_question.DeferredQuestion.dedupe_marker` is the
escalate-once guard, and the marker fingerprints the SET, so an unchanged set asks once and a
set that gains or loses a gate is a new decision rather than a silence.

Only a finding the report calls a FAULT — ``intent=UNDECIDED``, nobody decided to leave the gate
off — joins the batch. A ``STAGED`` entry cites the issue or date the call was made, so the
decision exists and asking again would be noise. That split is already in the data.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from teatree.config.gate_evidence import GateEvidence
from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.factory.feature_inertness import InertFeature

logger = logging.getLogger(__name__)

#: Namespaces the marker so a gate question can never collide with a repair one.
MARKER_PREFIX = "inert-gate:"


@dataclass(slots=True)
class InertGateQuestionScanner:
    """Raise one deduped question covering every gate finding nobody has decided."""

    #: Re-points the declaration away from the live registry — what lets a test declare a gate
    #: that has deliberately never shipped, and the only way to prove the expected set comes
    #: from the declaration rather than from the settings being measured.
    registry: Mapping[str, GateEvidence] | None = field(default=None)
    name: str = "inert_gate_questions"

    def scan(self) -> list[ScanSignal]:
        from teatree.core.factory.feature_inertness import feature_inertness  # noqa: PLC0415 — deferred: ORM-backed
        from teatree.core.models.deferred_question import (  # noqa: PLC0415 — deferred: ORM/app-registry
            DeferredQuestion,
            question_fingerprint,
        )

        try:
            undecided = sorted((f for f in feature_inertness(self.registry) if f.is_fault), key=lambda f: f.setting)
        except Exception:
            logger.exception("inert-gate scan failed — no question filed, no gate touched")
            return []

        if not undecided:
            return []

        settings = [finding.setting for finding in undecided]
        question = _question_text(undecided)
        try:
            DeferredQuestion.record(
                question,
                dedupe_marker=f"{MARKER_PREFIX}{question_fingerprint(' '.join(settings))}",
                audience=DeferredQuestion.Audience.OWNER_QUESTION,
            )
        except Exception:
            logger.exception("inert-gate question failed for %s", settings)
            return []
        return [
            ScanSignal(
                kind="gate.undecided",
                summary=question,
                payload={"settings": settings, "count": len(settings)},
            )
        ]


def _question_text(undecided: list["InertFeature"]) -> str:
    lines = "\n".join(f"  - {finding.setting}: {finding.detail}" for finding in undecided)
    return (
        f"{len(undecided)} shipped gates are off and nobody recorded a decision to leave them off. "
        f"For each: arm it, record it as deliberately staged with the reason, or delete it.\n{lines}"
    )


__all__ = ["MARKER_PREFIX", "InertGateQuestionScanner"]
