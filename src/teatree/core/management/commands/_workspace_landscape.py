"""The ``t3 <overlay> workspace landscape`` intake survey (#2541).

A thin re-export shim over :mod:`teatree.core.landscape_gather`, which owns the
gather composition. The composition lives in ``teatree.core`` (not here) so the
intake FSM worker (``execute_provision``) can import it to persist a
:class:`~teatree.core.models.landscape_artifact.LandscapeArtifact` without a
backwards ``core → management`` dependency edge. The ``workspace`` command imports
``LandscapeReport`` + ``run_landscape`` from here for backward compatibility.
"""

from teatree.core.landscape_gather import (
    LandscapeReport,
    OpenPrReport,
    RecommendationReport,
    WorktreeReport,
    run_landscape,
)

__all__ = [
    "LandscapeReport",
    "OpenPrReport",
    "RecommendationReport",
    "WorktreeReport",
    "run_landscape",
]
