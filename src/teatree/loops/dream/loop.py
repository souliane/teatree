"""``dream`` mini-loop — idle-time memory consolidation, off the live tick (#1933).

The dreaming consolidation pass is heavier than a scanner tick and must not
run on — or re-arm — the live 12-minute work loop (issue #1933 § 3). It is
registered as a MiniLoop so its cadence is configurable under ``[loops.dream]``
and the statusline can show its countdown, but it is marked ``off_live_tick``
so the live fan-out (:func:`teatree.loops.fanout.build_registry_jobs`) and the
:class:`teatree.loops.orchestrator.Orchestrator` skip it. The actual pass is
driven by its own low-frequency cron, the ``dream`` management command
(``t3 dream tick`` / ``t3 dream run``), which reuses the cadence ledger
(:class:`teatree.core.models.MiniLoopMarker`) and the in-flight lease
(:class:`teatree.core.models.LoopLease`).

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
import tomllib
from pathlib import Path
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
#:    explicit truthy value enables, an absent/unknown value defers to the toml.
#: 2. ``[loops.dream] <phase>`` in ``~/.teatree.toml`` — an explicit bool.
#:
#: Default (no env, no toml key) is ON, so each phase is live out of the box while
#: a single toml line (or a falsy env var) turns it off without a code change.
_FALSY = frozenset({"0", "false", "no", "off"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: One phase toggle: the ``[loops.dream]`` toml key and its ``T3_DREAM_*`` env var.
_PROPOSE_EVALS = ("propose_evals", "T3_DREAM_PROPOSE_EVALS")
_CROSS_LINK = ("cross_link", "T3_DREAM_CROSS_LINK")
_REINDEX = ("reindex", "T3_DREAM_REINDEX")
_DECAY = ("decay", "T3_DREAM_DECAY")


def _phase_enabled(toml_key: str, env_var: str, *, config_path: Path | None) -> bool:
    """Resolve a dream-phase toggle (default ON) across the env + toml kill-switch.

    The env layer wins when it carries an explicit truthy/falsy value; an absent
    or unrecognised env value defers to the ``[loops.dream] <toml_key>`` key, which
    itself defaults to ON. *config_path* overrides the toml location for tests.
    """
    raw_env = os.environ.get(env_var, "").strip().lower()
    if raw_env in _FALSY:
        return False
    if raw_env in _TRUTHY:
        return True
    return _toml_phase_enabled(toml_key, config_path)


def _toml_phase_enabled(toml_key: str, config_path: Path | None) -> bool:
    """Read ``[loops.dream] <toml_key>`` from the toml; default ON, never raise."""
    path = config_path if config_path is not None else Path.home() / ".teatree.toml"
    try:
        if not path.is_file():
            return True
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return True
    loops = data.get("loops")
    dream_table = loops.get("dream", {}) if isinstance(loops, dict) else {}
    value = dream_table.get(toml_key) if isinstance(dream_table, dict) else None
    return value if isinstance(value, bool) else True


def propose_evals_enabled(*, config_path: Path | None = None) -> bool:
    """Whether the nightly ``tick`` should request eval proposals (default ON)."""
    return _phase_enabled(*_PROPOSE_EVALS, config_path=config_path)


def cross_link_enabled(*, config_path: Path | None = None) -> bool:
    """Whether phase 4 (cross-link related memories) runs (default ON)."""
    return _phase_enabled(*_CROSS_LINK, config_path=config_path)


def reindex_enabled(*, config_path: Path | None = None) -> bool:
    """Whether phase 5 (regenerate ``MEMORY.md``) runs (default ON)."""
    return _phase_enabled(*_REINDEX, config_path=config_path)


def decay_enabled(*, config_path: Path | None = None) -> bool:
    """Whether phase 6 (decay/archive stale memories) runs (default ON)."""
    return _phase_enabled(*_DECAY, config_path=config_path)


#: The LLM-backed full-scenario derivation (#2447) is the one dream phase that is
#: default OFF — it makes a metered SDK call per candidate and stages real eval
#: files. Opt in with ``T3_DREAM_DERIVE_EVALS=1`` / ``[loops.dream] derive_evals =
#: true``; absent, the dream pass never invokes the LLM synthesizer (no behaviour
#: change). The deterministic ``promote`` path (default ON) is unaffected.
_DERIVE_EVALS = ("derive_evals", "T3_DREAM_DERIVE_EVALS")


def derive_evals_enabled(*, config_path: Path | None = None) -> bool:
    """Whether the LLM-backed full-scenario derivation runs (default OFF, #2447)."""
    raw_env = os.environ.get(_DERIVE_EVALS[1], "").strip().lower()
    if raw_env in _TRUTHY:
        return True
    if raw_env in _FALSY:
        return False
    return _toml_phase_disabled_by_default(_DERIVE_EVALS[0], config_path)


def _toml_phase_disabled_by_default(toml_key: str, config_path: Path | None) -> bool:
    """Read ``[loops.dream] <toml_key>`` from the toml; default OFF, never raise."""
    path = config_path if config_path is not None else Path.home() / ".teatree.toml"
    try:
        if not path.is_file():
            return False
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    loops = data.get("loops")
    dream_table = loops.get("dream", {}) if isinstance(loops, dict) else {}
    value = dream_table.get(toml_key) if isinstance(dream_table, dict) else None
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
