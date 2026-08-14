"""The operator report behind ``t3 doctor check``'s standing-merge-backlog finding (#4250).

Composes the shared population (:mod:`teatree.core.factory.merge_backlog`) with the
forge classifier (:mod:`teatree.core.merge.clear_liveness`) so the CLI surface holds
no classification logic and no prose — it echoes these lines and returns the verdict.

Two levels, deliberately different:

**FAIL** — the forge says the PR is OPEN, so the merge really is stalled. This is the
only class the watchdog digests into the owner DM, and it is now true by construction
rather than inferred from a missing local audit.

**WARN** — the PR settled outside the keystone, so the authorisation is spent. It is
real (the ledger is wrong and the reconciler should run) but it pages nobody and clears
itself on the next reconcile pass.

A row the classifier could not verify produces no line at all: no evidence is not a
finding.
"""

from dataclasses import dataclass
from datetime import datetime

from teatree.core.factory.merge_backlog import STALE_CLEAR_HOURS, UnconsumedClear, unconsumed_actionable_clear_rows
from teatree.core.merge.clear_liveness import PROBE_CAP, ClearLiveness, PrStateReader, probe, unverified_reader

#: How many backlog rows a finding names before it summarises the tail.
LISTED = 5

_RECONCILE_REMEDY = (
    "the PR settled outside the merge keystone, so the authorisation is spent — "
    "run `t3 <overlay> ticket reconcile-clears` to consume it."
)


@dataclass(frozen=True, slots=True)
class StaleClearReport:
    """The aged standing merge authorisations, split by what the forge says about each."""

    stalled: tuple[UnconsumedClear, ...] = ()
    settled: tuple[UnconsumedClear, ...] = ()
    unprobed: int = 0

    def lines(self) -> list[str]:
        rows: list[str] = []
        if self.stalled:
            rows.append(
                f"FAIL  {len(self.stalled)} merge authorisation(s) unconsumed past "
                f"{STALE_CLEAR_HOURS:.0f}h with the PR still open — oldest {self.stalled[0].describe()}. "
                "Each is a reviewed diff cleared to merge that never landed; "
                "re-issue the CLEAR at the live head or close the PR."
            )
            rows += _tail(self.stalled)
        if self.settled:
            rows.append(
                f"WARN  {len(self.settled)} merge authorisation(s) unconsumed past "
                f"{STALE_CLEAR_HOURS:.0f}h whose PR already merged or closed — "
                f"oldest {self.settled[0].describe()}. {_RECONCILE_REMEDY}"
            )
            rows += _tail(self.settled)
        if self.unprobed:
            rows.append(
                f"WARN  {self.unprobed} further aged authorisation(s) were not checked against the "
                f"forge this pass (probe capped at {PROBE_CAP}) — their state is unknown, not healthy."
            )
        return rows


def _tail(rows: tuple[UnconsumedClear, ...]) -> list[str]:
    lines = [f"      {row.describe()}" for row in rows[1:LISTED]]
    if len(rows) > LISTED:
        lines.append(f"      …and {len(rows) - LISTED} more standing authorisation(s).")
    return lines


def stale_clear_report(
    overlay: str,
    now: datetime,
    *,
    read: PrStateReader = unverified_reader,
    cap: int = PROBE_CAP,
) -> StaleClearReport:
    """Classify every standing authorisation older than :data:`STALE_CLEAR_HOURS`.

    Deliberately reads the GLOBAL population by default: a CLEAR whose repo no
    overlay declares is still a stalled merge, and scoping the report per overlay is
    how such a row went unreported for 19 days.
    """
    aged = [
        clear
        for clear in unconsumed_actionable_clear_rows(overlay)
        if (now - clear.issued_at).total_seconds() / 3600.0 > STALE_CLEAR_HOURS
    ]
    if not aged:
        return StaleClearReport()
    probed = probe(aged, read=read, cap=cap)
    return StaleClearReport(
        stalled=tuple(UnconsumedClear.of(clear, now) for clear in probed.of(ClearLiveness.STALLED)),
        settled=tuple(
            UnconsumedClear.of(clear, now) for clear in probed.of(ClearLiveness.MERGED, ClearLiveness.ABANDONED)
        ),
        unprobed=len(probed.unprobed),
    )
