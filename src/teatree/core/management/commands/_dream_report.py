"""Report clauses for one dream pass — the optional phrases its summary line is built from.

Split out of ``dream.py`` because assembling the human-readable line is a
presentation concern separate from driving the pass, and carrying the clauses as
six loose locals through the pass is what made that function unreadable.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.loops.dream.engine import DreamRunResult


@dataclass(frozen=True, slots=True)
class _ResultFragments:
    """The optional clauses a pass line is assembled from, each empty when it has nothing to say.

    Grouped so the three report sites name the same clauses instead of carrying
    six separate locals through the pass.
    """

    distilled: str
    evals: str
    empty: str
    rejected: str
    deferred: str
    broken: str

    @classmethod
    def of(cls, result: "DreamRunResult") -> "_ResultFragments":
        return cls(
            distilled=f"; distilled {result.snippets_distilled} snippet(s)" if result.snippets_distilled else "",
            evals=f"; {result.evals_proposed} eval candidate(s)" if result.evals_proposed else "",
            empty=(
                f"; WARN {result.empty_batches} batch(es) returned 0 clusters from non-empty input"
                if result.empty_batches
                else ""
            ),
            rejected=(
                f"; WARN {result.clusters_rejected} ungrounded cluster(s) rejected" if result.clusters_rejected else ""
            ),
            deferred=(
                f"; {result.deferred_members} snippet(s) DEFERRED to the next pass" if result.deferred_members else ""
            ),
            broken=(
                f"; FAILED {result.broken_batches} broken + {result.failed_batches} raised batch(es)"
                if result.distillation_broken
                else ""
            ),
        )
