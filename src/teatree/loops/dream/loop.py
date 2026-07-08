"""``dream`` mini-loop — idle-time memory consolidation, off the live tick (#1933).

The dreaming consolidation pass is heavier than a scanner tick and must not
run on — or re-arm — the live 12-minute work loop (issue #1933 § 3). It is
registered as a MiniLoop so the statusline can show its countdown, but it is
marked ``off_live_tick`` so the live fan-out
(:func:`teatree.loops.loop_table.build_loop_table_jobs`) skips it. The actual pass is
driven by its own low-frequency cron, the ``dream`` management command
(``t3 dream tick`` / ``t3 dream run``), which gates on the ONE cadence ledger —
the ``dream`` :class:`teatree.core.models.Loop` row's ``is_due`` / ``last_run_at``
(the same anchor every other loop's tick uses) — behind the in-flight
lease (:class:`teatree.core.models.LoopLease`).

The lease TTL is ``DREAM_LEASE_SECONDS``, sized above ``DREAM_PASS_BUDGET_SECONDS``
— the wall-clock budget cap for one consolidation pass — rather than the
``LoopLease.acquire`` 120s default. The pass is by design heavier than a scanner
tick (#1933 §3), so a default-leased pass running longer than 2min would silently
lose its lease mid-run and let a concurrent ``tick``/``run`` win the expired-lease
CAS — the overlap the "no two overlapping passes" invariant forbids. Matching the
TTL to the budget keeps the invariant true for the whole pass.

``build_jobs`` deliberately returns no scanner jobs — the consolidation engine
is invoked directly by the cron, not through the scanner-signal dispatch
pipeline.
"""

import os
from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob

DREAM_LOOP_NAME = "dream"
DREAM_LEASE_NAME = "dream-tick"
DREAM_DEFAULT_CADENCE_SECONDS = 24 * 3600  # nightly; the cron drives the actual ~04:00 firing.
DREAM_PASS_BUDGET_SECONDS = 30 * 60
DREAM_LEASE_SECONDS = DREAM_PASS_BUDGET_SECONDS + 5 * 60

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
_FALSY = frozenset({"0", "false", "no", "off"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: One phase toggle: the DB ``loops.dream`` key and its ``T3_DREAM_*`` env var.
_PROPOSE_EVALS = ("propose_evals", "T3_DREAM_PROPOSE_EVALS")
_CROSS_LINK = ("cross_link", "T3_DREAM_CROSS_LINK")
_MERGE = ("merge", "T3_DREAM_MERGE")
_REINDEX = ("reindex", "T3_DREAM_REINDEX")
_DECAY = ("decay", "T3_DREAM_DECAY")


def _dream_table() -> dict:
    """The ``dream`` sub-table of the DB ``loops`` setting; ``{}`` on absence/failure."""
    from teatree.config import cold_reader  # noqa: PLC0415

    loops = cold_reader.read_setting("loops")
    dream = loops.get("dream") if isinstance(loops, dict) else None
    return dream if isinstance(dream, dict) else {}


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
    value = _dream_table().get(key)
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


#: Phase 3c — the instruction-compliance accountant (#2663) — FILES enforcement
#: tickets for recurrences, so it is default OFF and ``--full``-gated, mirroring the
#: Pass-2 memory-promotion posture. Opt in with ``T3_DREAM_COMPLIANCE=1`` /
#: the DB ``loops.dream compliance = true`` key; absent, the dream pass never escalates or
#: persists a compliance snapshot (no behaviour change).
_COMPLIANCE = ("compliance", "T3_DREAM_COMPLIANCE")


def compliance_enabled() -> bool:
    """Whether phase-3c instruction-compliance accounting runs (default OFF, #2663)."""
    raw_env = os.environ.get(_COMPLIANCE[1], "").strip().lower()
    if raw_env in _TRUTHY:
        return True
    if raw_env in _FALSY:
        return False
    return _dream_phase_default_off(_COMPLIANCE[0])


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
    value = _dream_table().get(key)
    return value if isinstance(value, bool) else False


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    """No scanner jobs — the dream cron invokes the engine directly."""
    return []


MINI_LOOP = MiniLoop(
    name=DREAM_LOOP_NAME,
    default_cadence_seconds=DREAM_DEFAULT_CADENCE_SECONDS,
    build_jobs=_build_jobs,
    off_live_tick=True,
)
