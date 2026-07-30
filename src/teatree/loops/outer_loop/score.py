"""The ``read_score()`` seam — the outer loop's single metric-to-beat read (T4-PR-3).

One function over the T4-PR-2 recipe-weighted :func:`teatree.core.factory.factory_score.score`
so the loop's baseline snapshot, post-horizon measure, and no-regression predicate
all consume the SAME honest aggregate. Isolating the read here means a future
metric change is a one-function swap, not a scatter of call sites.
"""

from datetime import datetime

from teatree.config import get_effective_settings
from teatree.core.factory.factory_score import FactoryScore, score


def read_score(*, overlay: str = "", now: datetime | None = None) -> FactoryScore:
    """The recipe-weighted factory score for *overlay*, stamped against the approved sha."""
    settings = get_effective_settings(overlay or None)
    return score(overlay=overlay, now=now, approved_recipe_sha=settings.approved_recipe_sha)
