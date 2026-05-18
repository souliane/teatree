"""Phase 1 detector registry (BLUEPRINT § 5.7).

Only three detectors ship in Phase 1.  Keeping the tuple in a dedicated
module lets ``detectors/__init__.py`` stay a pure re-export surface,
while the structural test still has a single import path to enumerate
the full Phase 1 inventory.
"""

from teatree.loop.self_improve.detectors.base import SelfImproveDetector
from teatree.loop.self_improve.detectors.dispatch_gap import DispatchGapDetector
from teatree.loop.self_improve.detectors.forgotten_merge import ForgottenMergeDetector
from teatree.loop.self_improve.detectors.stale_statusline import StaleStatuslineEntryDetector

ALL_PHASE_1_DETECTORS: tuple[type[SelfImproveDetector], ...] = (
    DispatchGapDetector,
    ForgottenMergeDetector,
    StaleStatuslineEntryDetector,
)
