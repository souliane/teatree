"""Phase 1 detector registry (BLUEPRINT § 5.7).

Keeping the tuple in a dedicated module lets ``detectors/__init__.py`` stay
a pure re-export surface while structural tests enumerate the inventory.
"""

from teatree.loop.self_improve.detectors.base import SelfImproveDetector
from teatree.loop.self_improve.detectors.dispatch_gap import DispatchGapDetector
from teatree.loop.self_improve.detectors.forgotten_merge import ForgottenMergeDetector
from teatree.loop.self_improve.detectors.lifecycle_incident import LifecycleIncidentDetector
from teatree.loop.self_improve.detectors.loop_health import LoopHealthDetector
from teatree.loop.self_improve.detectors.pressure_incident import PressureIncidentDetector
from teatree.loop.self_improve.detectors.skill_assurance_gap import SkillAssuranceGapDetector
from teatree.loop.self_improve.detectors.stale_statusline import StaleStatuslineEntryDetector
from teatree.loop.self_improve.detectors.telemetry_action_gap import TelemetryActionGapDetector

ALL_PHASE_1_DETECTORS: tuple[type[SelfImproveDetector], ...] = (
    DispatchGapDetector,
    ForgottenMergeDetector,
    LifecycleIncidentDetector,
    LoopHealthDetector,
    PressureIncidentDetector,
    SkillAssuranceGapDetector,
    StaleStatuslineEntryDetector,
    TelemetryActionGapDetector,
)
