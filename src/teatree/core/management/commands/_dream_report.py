"""Report clauses for one dream pass — the optional phrases its summary line is built from.

Split out of ``dream.py`` because assembling the human-readable line is a
presentation concern separate from driving the pass, and carrying the clauses as
six loose locals through the pass is what made that function unreadable.
"""

import operator
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

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
    budget_stopped: str

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
            budget_stopped=(
                f"; STOPPED distilling on the pass budget with {result.budget_stopped_batches} "
                "selected batch(es) unreached"
                if result.budget_stopped_batches
                else ""
            ),
        )


@dataclass
class TailTimings:
    """Per-phase wall clock for the pass's TAIL — everything after the distiller.

    The tail is where a pass dies: the distiller stops on its own budget and logs that it
    did, then the tail runs unmetered until the external deadline SIGKILLs the process
    group before the gates or the marker are reached (#4671). The pass emitted no per-phase
    timing, so which tail phase consumed the time was unattributable from the logs — every
    default-ON phase measures ~12s in isolation against the live corpus, so the sink is not
    the tail's business logic and only in-situ timing can name it.

    *clock* is injected so a test can assert the clause without sleeping.
    """

    clock: Callable[[], float] = field(default=time.monotonic)
    phases: list[tuple[str, float]] = field(default_factory=list)

    @contextmanager
    def phase(self, label: str) -> "Iterator[None]":
        """Time one tail phase, recording it even when the phase raises."""
        started = self.clock()
        try:
            yield
        finally:
            self.phases.append((label, self.clock() - started))

    @property
    def summary(self) -> str:
        """The ``; tail Ns (phase Ns, …)`` clause, slowest phase first."""
        if not self.phases:
            return ""
        total = sum(elapsed for _, elapsed in self.phases)
        ranked = sorted(self.phases, key=operator.itemgetter(1), reverse=True)
        detail = ", ".join(f"{label} {elapsed:.0f}s" for label, elapsed in ranked)
        return f"; tail {total:.0f}s ({detail})"
