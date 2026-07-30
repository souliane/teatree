"""The SHA-bind predicate: a merge may only execute against the cleared tree.

The canonical, reusable equality check the merge keystone's SHA-bind gate runs
(§17.4.3 step 2): a merge executes only against the exact head SHA the review
clearance was recorded at. Any new push moves the live head off the cleared SHA
and this returns ``False`` — clearance is invalidated until re-cleared. Extracted
so the gate is a named, enumerable entry in the chokepoint registry
(:mod:`teatree.core.factory.chokepoint_registry`) instead of an anonymous ``!=`` buried
in the precondition orchestration.
"""


def verify_sha_bound(cleared_sha: str, live_sha: str) -> bool:
    """True iff the live head SHA equals the SHA the clearance was recorded at.

    Both sides are canonicalised to lowercase full hex at the boundary, so a
    mixed-case forge ``headRefOid`` can never silently fail the bind. An empty
    ``cleared_sha`` or ``live_sha`` is never bound — a blank SHA vouches for
    nothing.
    """
    cleared = cleared_sha.strip().lower()
    live = live_sha.strip().lower()
    return bool(cleared) and cleared == live
