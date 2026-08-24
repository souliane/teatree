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

import os
from typing import TYPE_CHECKING

from teatree.loops.base import LoopDeterminism, LoopReach, MiniLoop
from teatree.loops.dream.pass_config import FALSY as _FALSY
from teatree.loops.dream.pass_config import TRUTHY as _TRUTHY
from teatree.loops.dream.pass_config import dream_table

if TYPE_CHECKING:
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

#: Every dream phase that can be turned off is LIVE by default (#2346 "make it
#: live", #1933 phases 4-6). Each carries the SAME two-layer kill-switch, first
#: match wins:
#:
#: 1. ``T3_DREAM_<PHASE>`` env — ``0``/``false``/``no``/``off`` disables, an
#:    explicit truthy value enables, an absent/unknown value defers to the DB.
#: 2. the ``dream`` sub-table of the DB ``loops`` setting, key ``<phase>`` — an
#:    explicit bool.
#:
#: Default (no env, no DB key) is ON, so each phase is live out of the box while
#: a single ``config_setting set`` (or a falsy env var) turns it off.

#: One phase toggle: the DB ``loops.dream`` key and its ``T3_DREAM_*`` env var.
_PROPOSE_EVALS = ("propose_evals", "T3_DREAM_PROPOSE_EVALS")
_CROSS_LINK = ("cross_link", "T3_DREAM_CROSS_LINK")
_MERGE = ("merge", "T3_DREAM_MERGE")
_REINDEX = ("reindex", "T3_DREAM_REINDEX")
_DECAY = ("decay", "T3_DREAM_DECAY")


def _phase_enabled(key: str, env_var: str) -> bool:
    """Resolve a dream-phase toggle (default ON) across the env + DB kill-switch.

    The env layer wins when it carries an explicit truthy/falsy value; an absent
    or unrecognised env value defers to the DB ``loops.dream`` key, default ON.
    """
    raw_env = os.environ.get(env_var, "").strip().lower()
    if raw_env in _FALSY:
        return False
    if raw_env in _TRUTHY:
        return True
    value = dream_table().get(key)
    return value if isinstance(value, bool) else True


def propose_evals_enabled() -> bool:
    """Whether the nightly ``tick`` should request eval proposals (default ON)."""
    return _phase_enabled(*_PROPOSE_EVALS)


def cross_link_enabled() -> bool:
    """Whether phase 4 (cross-link related memories) runs (default ON)."""
    return _phase_enabled(*_CROSS_LINK)


def merge_enabled() -> bool:
    """Whether phase 4b (merge near-duplicate memories) runs (default ON, #2723)."""
    return _phase_enabled(*_MERGE)


def reindex_enabled() -> bool:
    """Whether phase 5 (regenerate ``MEMORY.md``) runs (default ON)."""
    return _phase_enabled(*_REINDEX)


def decay_enabled() -> bool:
    """Whether phase 6 (decay/archive stale memories) runs (default ON)."""
    return _phase_enabled(*_DECAY)


#: Pass-2 memory promotion (#2426) FILES backlog tickets, so it is default OFF —
#: opt in with ``T3_DREAM_MEMORY_PROMOTE=1`` / the DB ``loops.dream memory_promote =
#: true`` key. Absent, the dream pass never triages the ledger or files a ticket (no
#: behaviour change).
_MEMORY_PROMOTE = ("memory_promote", "T3_DREAM_MEMORY_PROMOTE")


def memory_promote_enabled() -> bool:
    """Whether Pass-2 memory→fix promotion runs (default OFF, #2426)."""
    raw_env = os.environ.get(_MEMORY_PROMOTE[1], "").strip().lower()
    if raw_env in _TRUTHY:
        return True
    if raw_env in _FALSY:
        return False
    return _dream_phase_default_off(_MEMORY_PROMOTE[0])


#: The LLM-backed full-scenario derivation (#2447) is the one dream phase that is
#: default OFF — it makes a metered SDK call per candidate and stages real eval
#: files. Opt in with ``T3_DREAM_DERIVE_EVALS=1`` / the DB ``loops.dream derive_evals =
#: true`` key; absent, the dream pass never invokes the LLM synthesizer (no behaviour
#: change). The deterministic ``promote`` path (default ON) is unaffected.
_DERIVE_EVALS = ("derive_evals", "T3_DREAM_DERIVE_EVALS")


def derive_evals_enabled() -> bool:
    """Whether the LLM-backed full-scenario derivation runs (default OFF, #2447)."""
    raw_env = os.environ.get(_DERIVE_EVALS[1], "").strip().lower()
    if raw_env in _TRUTHY:
        return True
    if raw_env in _FALSY:
        return False
    return _dream_phase_default_off(_DERIVE_EVALS[0])


#: Phase 3c is SPLIT into two independently-gated halves (#2663). MEASUREMENT — the
#: root-KPI accountant — only PERSISTS a compliance snapshot (never files), so it is
#: default ON and runs on EVERY pass: the root KPI must actually be measured. Two-layer
#: kill-switch (env then DB ``loops.dream compliance_measure``), default ON, so a single
#: falsy env / DB key turns measurement off.
_COMPLIANCE_MEASURE = ("compliance_measure", "T3_DREAM_COMPLIANCE_MEASURE")


def compliance_measure_enabled() -> bool:
    """Whether phase-3c compliance MEASUREMENT runs (default ON, #2663)."""
    return _phase_enabled(*_COMPLIANCE_MEASURE)


#: ESCALATION — the other half — FILES enforcement tickets for recurrences, so it is
#: default OFF, mirroring the Pass-2 memory-promotion posture. Opt in with
#: ``T3_DREAM_COMPLIANCE_ESCALATE=1`` / the DB ``loops.dream compliance_escalate = true``
#: key; absent, the dream pass measures but never escalates (no ticket-filing). The
#: toggle ALONE suffices: it used to be ANDed with ``--full`` at the call site, which the
#: cron ``tick`` can never set, so the toggle was dead on the nightly path (#4176).
_COMPLIANCE_ESCALATE = ("compliance_escalate", "T3_DREAM_COMPLIANCE_ESCALATE")


def compliance_escalate_enabled() -> bool:
    """Whether phase-3c compliance ESCALATION runs (default OFF, #2663)."""
    raw_env = os.environ.get(_COMPLIANCE_ESCALATE[1], "").strip().lower()
    if raw_env in _TRUTHY:
        return True
    if raw_env in _FALSY:
        return False
    return _dream_phase_default_off(_COMPLIANCE_ESCALATE[0])


#: Phase 3d — the automatable-ask promoter (#2663), the "improve-with-new-stuff"
#: sibling of the compliance accountant. It PROMOTES recurring manual user asks to a
#: fix-and-merge (a checkbox + scheduled coding task). Gated by an OR at the call site
#: (``if not force_all_phases and not automation_asks_enabled()``): it runs on ``--full``
#: OR when opted in with ``T3_DREAM_AUTOMATION_ASKS=1`` / the DB ``loops.dream automation_asks
#: = true`` key — so ``--full`` alone triggers it, whereas the compliance phase's AND-gate
#: additionally requires its own toggle even under ``--full``. Absent both, the dream
#: pass never promotes an ask (no behaviour change).
_AUTOMATION_ASKS = ("automation_asks", "T3_DREAM_AUTOMATION_ASKS")


def automation_asks_enabled() -> bool:
    """Whether phase-3d automatable-ask promotion runs (default OFF, #2663)."""
    raw_env = os.environ.get(_AUTOMATION_ASKS[1], "").strip().lower()
    if raw_env in _TRUTHY:
        return True
    if raw_env in _FALSY:
        return False
    return _dream_phase_default_off(_AUTOMATION_ASKS[0])


def _dream_phase_default_off(key: str) -> bool:
    """Read the DB ``loops.dream`` key; default OFF, never raise."""
    value = dream_table().get(key)
    return value if isinstance(value, bool) else False


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
