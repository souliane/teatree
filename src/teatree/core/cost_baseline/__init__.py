"""The frozen pre-Opus-5 per-phase cost baseline and the comparison it enables.

``aggregate`` turns real dispatch attempts into per-phase / per-``(phase, model)``
distributions; ``frozen`` loads the committed baseline and diffs a live
aggregate against it. The committed artifact is ``pre_opus5.yaml``, next to the
loader that reads it.
"""

from teatree.core.cost_baseline.aggregate import (
    AttemptRecord,
    UsageStats,
    aggregate_by_phase,
    aggregate_by_phase_model,
    percentile,
)
from teatree.core.cost_baseline.frozen import (
    FROZEN_BASELINE_PATH,
    BaselineError,
    FrozenBaseline,
    PhaseComparison,
    compare_phases,
    load_frozen_baseline,
)

__all__ = [
    "FROZEN_BASELINE_PATH",
    "AttemptRecord",
    "BaselineError",
    "FrozenBaseline",
    "PhaseComparison",
    "UsageStats",
    "aggregate_by_phase",
    "aggregate_by_phase_model",
    "compare_phases",
    "load_frozen_baseline",
    "percentile",
]
