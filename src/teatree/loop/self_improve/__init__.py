"""Self-improving monitor for the autonomous factory (BLUEPRINT § 5.7).

Phase 1 surface: a tier-dispatched cadence shell that runs three cheap
detectors (``DispatchGapDetector``, ``ForgottenMergeDetector``,
``StaleStatuslineEntryDetector``), dedups firings against the durable
``SelfImproveFiring`` table, and reacts via a five-rung action ladder
bounded per-detector.

Reuses the existing scanner protocol (``loop/scanners/base.py``) — every
detector is a ``Scanner`` plus a ``DetectorReport`` with the dedup key,
state hash, ladder ceiling, and ``auto_fix`` flag the schedule + actions
modules consult.
"""

from teatree.loop.self_improve.actions import ActionResult, ActionRung, format_slack_payload, run_action_ladder
from teatree.loop.self_improve.budget import BudgetVerdict, precheck_budget
from teatree.loop.self_improve.dedup import canonical_key, state_hash
from teatree.loop.self_improve.detectors.base import DetectorReport, SelfImproveDetector
from teatree.loop.self_improve.persistence import SLACK_RATE_CAP_SECONDS, recent_slack_firings_within, record_firing
from teatree.loop.self_improve.schedule import Tier, run_tier

__all__ = [
    "SLACK_RATE_CAP_SECONDS",
    "ActionResult",
    "ActionRung",
    "BudgetVerdict",
    "DetectorReport",
    "SelfImproveDetector",
    "Tier",
    "canonical_key",
    "format_slack_payload",
    "precheck_budget",
    "recent_slack_firings_within",
    "record_firing",
    "run_action_ladder",
    "run_tier",
    "state_hash",
]
