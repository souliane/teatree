"""Re-export shim for :class:`MergeGuard`, now owned by :mod:`teatree.core.gates.merge_guard`.

The campaign PR5 god-module splits moved the 17 phase/ship gates into the
``teatree.core.gates`` package. A registered overlay consumer imports
``MergeGuard`` from the old flat ``teatree.core.merge_guard`` path; this shim
keeps that import resolving so the split lands with no cross-repo lockstep —
the overlay repoints to ``teatree.core.gates.merge_guard`` in its own follow-up
after this PR merges, and this shim is deleted then.

Same shape as :mod:`teatree.backends.protocols` (the PR3 re-export shim, also
PR7-deletable). SCHEDULED FOR DELETION once every registered overlay consumer
has repointed to ``teatree.core.gates.merge_guard``.
"""

from teatree.core.gates.merge_guard import MergeGuard

__all__ = ["MergeGuard"]
