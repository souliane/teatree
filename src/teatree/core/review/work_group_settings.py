"""The settings seam for :mod:`teatree.core.review.work_group`.

Kept out of the grouper itself so that module stays pure — stdlib only, no config
read, no Django — and remains testable as a table with the scope filter passed
in. This is the ONE place ``work_group_generic_scopes`` becomes the frozenset the
grouper takes, so two callers cannot disagree about which conventional-commit
scopes are too generic to fuse unrelated merge requests on.
"""

from teatree.config import get_effective_settings


def generic_scopes_from_settings(overlay: str = "") -> frozenset[str]:
    return frozenset(get_effective_settings(overlay or None).work_group_generic_scopes)
