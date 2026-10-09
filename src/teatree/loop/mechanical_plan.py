"""The plan object the mechanical resource passes share, and how it reaches the operator.

Two passes run on the resource mini-loop — the destructive ``free_resources`` ladder and
the loss-free artifact sweep — on different cadences and with different authority. They
agree on exactly one thing: what a PLAN is, and that a pass which reports nothing is
indistinguishable from one that did nothing. That agreement lives here so neither pass
owns the other's primitives and a pass added later inherits the reporting contract
rather than reinventing it (#4244).
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from teatree.core.models.resource_pressure_marker import ResourcePressureMarker

logger = logging.getLogger(__name__)

GIB = 1024 * 1024 * 1024

# How many per-item lines one plan section prints before summarising the rest.
# The plan is read by a human on a full disk; hundreds of keep-lines would bury
# the counts that say whether the pass did anything.
PLAN_SAMPLE = 5


@dataclass(slots=True)
class FreePlan:
    """The computed freeing plan for one pass — persisted before execution."""

    resource: str
    steps: list[str] = field(default_factory=list)
    estimated_reclaim_gb: float = 0.0
    reclaimed_gb: float = 0.0

    def render(self) -> str:
        head = (
            f"[{timezone.now().isoformat()}] resource={self.resource} "
            f"est={self.estimated_reclaim_gb:.2f}GB reclaimed={self.reclaimed_gb:.2f}GB"
        )
        return head + "\n" + "\n".join(f"  - {s}" for s in self.steps)


def persist_plan(marker: "ResourcePressureMarker", plan: FreePlan, *, field_name: str, caller: str) -> None:
    """Write *plan* to the marker field that pass OWNS, and log under that pass's name.

    Both passes persist a plan, on different cadences. Sharing one field means whichever
    ran last is the only account that survives, so the other pass's record — including
    what its delete-time guard stopped, the whole point of reporting it — is silently
    gone. And a hardcoded caller name on the failure log sends an operator reading it
    after one pass to the other pass entirely (#4244).
    """
    try:
        setattr(marker, field_name, plan.render())
        marker.save(update_fields=[field_name])
    except Exception:
        logger.exception("%s: failed to persist plan", caller)


def sampled(lines: tuple[str, ...]) -> list[str]:
    """The first few lines plus an honest trailer — a plan nobody reads reports nothing."""
    if len(lines) <= PLAN_SAMPLE:
        return list(lines)
    return [*lines[:PLAN_SAMPLE], f"… and {len(lines) - PLAN_SAMPLE} more"]


def append_stopped_deletions(plan: FreePlan, what: str, refusal: str, skipped: tuple[str, ...]) -> None:
    """Record what the delete-time guard stopped — a silent skip is the defect class itself."""
    if refusal:
        plan.steps.append(f"ABORT {what} at deletion time — {refusal}")
    for line in sampled(skipped):
        plan.steps.append(f"  SKIP {line}")


__all__ = ["GIB", "PLAN_SAMPLE", "FreePlan", "append_stopped_deletions", "persist_plan", "sampled"]
