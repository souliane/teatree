"""``dream`` mini-loop — idle-time memory consolidation, off the live tick (#1933).

The dreaming consolidation pass is heavier than a scanner tick and must not
run on — or re-arm — the live 12-minute work loop (issue #1933 § 3). It is
registered as a MiniLoop so the statusline can show its countdown, but it is
marked ``off_live_tick`` so the live fan-out
(:func:`teatree.loops.loop_table.build_loop_table_jobs`) skips it. The actual pass is
driven by the worker's off-live-tick driver chain
(:func:`teatree.loops.off_live_tick_driver.drive_off_live_tick_loops`), which fires the
``off_tick_command`` below — the ``dream`` management command
(``t3 dream tick`` / ``t3 dream run``) — which gates on the ONE cadence ledger —
the ``dream`` :class:`teatree.core.models.Loop` row's ``is_due`` / ``last_run_at``
(the same anchor every other loop's tick uses) — behind the in-flight
lease (:class:`teatree.core.models.LoopLease`).

Three timings bound one pass, and they must stay strictly ordered — the whole
learning loop was dead because two of them were EQUAL:

1.  ``DREAM_PASS_BUDGET_SECONDS`` — the IN-PASS budget. The pass reads its own clock
    against this (:class:`~teatree.loops.dream.pass_config.PassBudget`) and stops
    launching new distiller batches while ``DREAM_TAIL_RESERVE_SECONDS`` is still on
    it, so it ends by DECISION with its tail intact.
2.  ``DREAM_OFF_TICK_DEADLINE_SECONDS`` — the EXTERNAL ceiling the driver enforces
    with a SIGKILL, declared per-loop on the :class:`MiniLoop` below. It sits a clear
    margin ABOVE the in-pass budget so it is a genuine last-resort backstop. It used
    to equal the budget (both 1800s, the shared ``DAILY_TICK_DEADLINE_SECONDS``), and
    with nothing inside the pass reading a clock the SIGKILL was the ONLY way a pass
    ever ended — always mid-distil, so compliance, the §4 acceptance gates, phases
    4-6, Pass-2 promotion and ``mark_succeeded`` were all unreachable.
3.  ``DREAM_LEASE_SECONDS`` — the in-flight lease TTL, above BOTH, rather than the
    ``LoopLease.acquire`` 120s default. The pass is by design heavier than a scanner
    tick (#1933 §3), so a default-leased pass running longer than 2min would silently
    lose its lease mid-run and let a concurrent ``tick``/``run`` win the expired-lease
    CAS — the overlap the "no two overlapping passes" invariant forbids. The TTL must
    cover the deadline, not merely the budget: a pass in the margin between them is
    still running and still holds the invariant.

``DREAM_RETRY_BACKOFF_SECONDS`` bounds the fourth failure mode. ``Loop.is_due`` keys on
``last_run_at`` and only a STAMPED pass bumps it (#2285's retry-until-success), so a
pass that ends without stamping leaves the loop due and the 600s driver chain relaunches
it within one fire — measured at 48 passes a day, each re-spending metered distiller
calls. Requiring the backoff since ``last_attempt_at`` (stamped BEFORE the pass, so it
survives a SIGKILL) keeps retry-until-success while bounding the retry rate.

``build_jobs`` deliberately returns no scanner jobs — the consolidation engine
is invoked directly by the tick command, not through the scanner-signal dispatch
pipeline.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import LoopDeterminism, LoopReach, MiniLoop

if TYPE_CHECKING:
    from teatree.config.settings import UserSettings
    from teatree.loop.job_identity import _ScannerJob

DREAM_LOOP_NAME = "dream"
DREAM_LEASE_NAME = "dream-tick"
DREAM_DEFAULT_CADENCE_SECONDS = 24 * 3600  # nightly; the driver chain fires the actual ~04:00 pass.
DREAM_PASS_BUDGET_SECONDS = 30 * 60

#: What the pass keeps back for everything AFTER the distiller: compliance measurement,
#: the automatable-ask and Pass-2 promoters, phases 4-6 (cross-link / re-index / decay),
#: the §4 acceptance gates and the marker. Measured 2026-08-20 on the live deploy under
#: load: phases 4-6 + gates 77.4s, compliance measurement 1.5s — about 80s of
#: deterministic work. The reserve is set at 6x that, because the promoting phases are
#: currently OFF and each makes forge round-trips once enabled, and because the cost of
#: over-reserving is cheap and self-correcting (a few distiller batches deferred to the
#: next pass through the rotation cursor) while the cost of under-reserving is the
#: defect this constant exists to remove — the entire tail lost.
DREAM_TAIL_RESERVE_SECONDS = 8 * 60

#: The EXTERNAL ceiling for one ``dream tick`` subprocess, declared to the off-live-tick
#: driver via ``MiniLoop.off_tick_deadline_seconds``. Deliberately ABOVE the in-pass
#: budget so the SIGKILL is a backstop for a pass that ignores its own clock, not the
#: routine exit path.
DREAM_OFF_TICK_DEADLINE_SECONDS = DREAM_PASS_BUDGET_SECONDS + 10 * 60

DREAM_LEASE_SECONDS = DREAM_OFF_TICK_DEADLINE_SECONDS + 5 * 60

#: The floor between two dream pass ATTEMPTS. Applied on top of ``Loop.is_due`` in the
#: ``tick`` cadence gate, against ``Loop.last_attempt_at`` (#4355, migration 0075) —
#: which the command stamps before the pass, so it survives a SIGKILL where
#: ``last_run_at`` does not.
DREAM_RETRY_BACKOFF_SECONDS = 2 * 3600

#: Each dream phase is a declared setting (``teatree.config.settings_loop_owned``), resolved
#: through the ordinary chain — ``T3_DREAM_<PHASE>`` env, then the ``ConfigSetting``
#: store, then the shipped default. The phases that only read and rewrite memory ship ON;
#: every phase that files a ticket or makes a metered model call ships OFF.


def _settings() -> "UserSettings":
    """The effective settings, resolved at call time so a phase never binds a stale read."""
    from teatree.loops.dream.pass_config import dream_settings  # noqa: PLC0415 — deferred: ORM-backed read

    return dream_settings()


def propose_evals_enabled() -> bool:
    """Whether the nightly ``tick`` should request eval proposals (default ON)."""
    settings = _settings()
    return settings.dream_propose_evals


def cross_link_enabled() -> bool:
    """Whether phase 4 (cross-link related memories) runs (default ON)."""
    settings = _settings()
    return settings.dream_cross_link


def merge_enabled() -> bool:
    """Whether phase 4b (merge near-duplicate memories) runs (default ON, #2723)."""
    settings = _settings()
    return settings.dream_merge


def reindex_enabled() -> bool:
    """Whether phase 5 (regenerate ``MEMORY.md``) runs (default ON)."""
    settings = _settings()
    return settings.dream_reindex


def decay_enabled() -> bool:
    """Whether phase 6 (decay/archive stale memories) runs (default ON)."""
    settings = _settings()
    return settings.dream_decay


def memory_promote_enabled() -> bool:
    """Whether Pass-2 memory→fix promotion runs (default ON — a pass batches its promotions into ONE ticket, #4776)."""
    settings = _settings()
    return settings.dream_memory_promote


def derive_evals_enabled() -> bool:
    """Whether the LLM-backed full-scenario derivation runs (default OFF — metered, #2447)."""
    settings = _settings()
    return settings.dream_derive_evals


def compliance_measure_enabled() -> bool:
    """Whether phase-3c compliance MEASUREMENT runs (default ON — it persists, never files, #2663)."""
    settings = _settings()
    return settings.dream_compliance_measure


def compliance_escalate_enabled() -> bool:
    """Whether phase-3c compliance ESCALATION runs (default OFF — it FILES enforcement tickets, #2663).

    The toggle ALONE suffices: it used to be ANDed with ``--full`` at the call site, which
    the cron ``tick`` can never set, so the toggle was dead on the nightly path (#4176).
    """
    settings = _settings()
    return settings.dream_compliance_escalate


def automation_asks_enabled() -> bool:
    """Whether phase-3d automatable-ask promotion runs (default OFF — it schedules work, #2663).

    Gated by an OR at the call site (``if not force_all_phases and not
    automation_asks_enabled()``), so ``--full`` alone triggers it — whereas the compliance
    phase's AND-gate additionally requires its own toggle even under ``--full``.
    """
    settings = _settings()
    return settings.dream_automation_asks


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    """No scanner jobs — the dream tick command invokes the engine directly."""
    return []


MINI_LOOP = MiniLoop(
    name=DREAM_LOOP_NAME,
    default_cadence_seconds=DREAM_DEFAULT_CADENCE_SECONDS,
    build_jobs=_build_jobs,
    off_live_tick=True,
    off_tick_command=("dream", "tick"),
    off_tick_deadline_seconds=DREAM_OFF_TICK_DEADLINE_SECONDS,
    declared_reach=frozenset({LoopReach.EGRESS}),
    determinism=LoopDeterminism.AI,
)
