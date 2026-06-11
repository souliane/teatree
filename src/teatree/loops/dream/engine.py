"""Distillation-engine SEAM for the idle-time dream pass (#1933).

This module is the single, well-named entry point the dream cron exercises so
the whole orchestration around it — the in-flight lease, the ``--dry-run``
no-write path, ``DreamRunMarker`` stamping, and the staleness alarm — is fully
testable WITHOUT an LLM. The real engine is a follow-up PR; this scaffold ships
an inert stub.

TODO(#1933): implement the consolidation engine. Per the issue's § 2 phases,
adapted from arXiv:2606.03979 (schedule + safety ordering only — no weight
updates):

1. Replay / re-read — re-read the raw source memory files + recent session
    signal (retro findings, user-correction memories highest-weight, cold
    reviews, deny-streaks, …), not summaries-of-summaries.
2. Dedup / merge / cluster — group entries sharing one root cause.
3. Distill — one imperative rule per cluster; an LLM rewrite of markdown,
    never a verbatim copy of episodes. Verify-before-durable-write: a rule is
    written only if it would have prevented a real, CITED mistake
    (``ConsolidatedMemory.mark_verified`` refuses an empty citation).
4. (Optional) Dream / cross-link — low-temperature pass surfacing latent
    shared root causes across unrelated entries.
5. Re-index — rewrite ``MEMORY.md`` so each surviving cluster has a <=1-line
    entry, bringing the index back under its load budget.
6. Decay / archive (prune) — remove a volatile index line ONLY after the fact
    has a confirmed durable home (transfer-before-prune); archive, never
    hard-delete; BINDING feedback is never silently dropped
    (``ConsolidatedMemory.expire`` raises ``BindingFeedbackError``).

The output store is the **DB-backed ``ConsolidatedMemory`` ledger** (the
canonical state + audit trail; the rendered topic file is the durable
destination) — user-decided, NOT in-place file rewrite.

Deferred (do NOT decide here — see issue #1933 § 6 open questions):
- The QA-probe corpus source feeding the retention/interference/monotonicity
    gates (``DreamQaProbe``) and whether/how it is persisted across runs.
- The exact archive location and the restore-on-recurrence trigger.
- Whether the phase-4 cross-link pass ships in v1 or is deferred.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class DreamRunResult:
    """Outcome of one consolidation pass — the cron renders + the marker keys on it.

    ``clusters_recorded`` is the count of ``ConsolidatedMemory`` rows the pass
    recorded (0 from the current stub); ``members_replayed`` is the count of
    source signal members the replay phase read. ``dry_run`` echoes the
    requested mode so the caller can confirm no row was written.
    """

    clusters_recorded: int
    members_replayed: int
    dry_run: bool


def run_consolidation(*, overlay: str, since: datetime | None, dry_run: bool) -> DreamRunResult:
    """Run one consolidation pass for *overlay* (STUB — engine is a #1933 seam).

    *since* bounds the recent-session-signal replay window (``None`` = the
    engine's own default lookback); *dry_run* must do everything except writing
    ``ConsolidatedMemory`` rows. The current stub is a no-op: it records no
    clusters and writes no rows in either mode, so the cron orchestration can
    be exercised end to end with no LLM. See the module docstring for the full
    phase plan and the deferred design questions.
    """
    del overlay, since  # consumed by the real engine; named for the seam contract.
    return DreamRunResult(clusters_recorded=0, members_replayed=0, dry_run=dry_run)
