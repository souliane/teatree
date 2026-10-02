"""Phase 1 self-improve detectors (BLUEPRINT § 5.7).

Only three detectors ship in this phase — the smell catalogue in the
issue plan defines the full list, but only the items below carry working
implementations.  Phase 2 / Phase 3 detectors are stubbed in BLUEPRINT
and intentionally absent from this package.
"""

from teatree.loop.self_improve.detectors.base import DetectorReport, SelfImproveDetector
from teatree.loop.self_improve.detectors.dispatch_gap import DispatchGapDetector
from teatree.loop.self_improve.detectors.forgotten_merge import ForgottenMergeDetector
from teatree.loop.self_improve.detectors.lifecycle_incident import LifecycleIncidentDetector
from teatree.loop.self_improve.detectors.pressure_incident import PressureIncidentDetector
from teatree.loop.self_improve.detectors.registry import ALL_PHASE_1_DETECTORS
from teatree.loop.self_improve.detectors.skill_assurance_gap import SkillAssuranceGapDetector
from teatree.loop.self_improve.detectors.stale_statusline import StaleStatuslineEntryDetector
from teatree.loop.self_improve.detectors.telemetry_action_gap import TelemetryActionGapDetector

__all__ = [
    "ALL_PHASE_1_DETECTORS",
    "DetectorReport",
    "DispatchGapDetector",
    "ForgottenMergeDetector",
    "LifecycleIncidentDetector",
    "PressureIncidentDetector",
    "SelfImproveDetector",
    "SkillAssuranceGapDetector",
    "StaleStatuslineEntryDetector",
    "TelemetryActionGapDetector",
]
