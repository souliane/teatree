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

#: The nightly eval-derivation seam is LIVE by default (#2346 "make it live"): the
#: cadence-driven ``tick`` requests eval proposals unless explicitly disabled. The
#: kill-switch is two-layered, first match wins:
#:
#: 1. ``T3_DREAM_PROPOSE_EVALS`` env — ``0``/``false``/``no`` disables, anything
#:    else (incl. absent) defers to the toml layer; truthy explicitly enables.
#: 2. ``[loops.dream] propose_evals`` in ``~/.teatree.toml`` — an explicit bool.
#:
#: Default (no env, no toml key) is ON, so the seam is live out of the box while a
#: single toml line (or a falsy env var) turns it off without a code change.
_DEFAULT_PROPOSE_EVALS = True
_ENV_PROPOSE_EVALS = "T3_DREAM_PROPOSE_EVALS"
_FALSY = frozenset({"0", "false", "no", "off"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def propose_evals_enabled(*, config_path: Path | None = None) -> bool:
    """Resolve whether the nightly ``tick`` should request eval proposals (default ON).

    The env layer wins when it carries an explicit truthy/falsy value; an absent
    or unrecognised env value defers to the ``[loops.dream] propose_evals`` toml
    key, which itself defaults to :data:`_DEFAULT_PROPOSE_EVALS` (ON). *config_path*
    overrides the toml location for tests.
    """
    raw_env = os.environ.get(_ENV_PROPOSE_EVALS, "").strip().lower()
    if raw_env in _FALSY:
        return False
    if raw_env in _TRUTHY:
        return True
    return _toml_propose_evals(config_path)


def _toml_propose_evals(config_path: Path | None) -> bool:
    """Read ``[loops.dream] propose_evals`` from the toml; default ON, never raise."""
    path = config_path if config_path is not None else Path.home() / ".teatree.toml"
    try:
        if not path.is_file():
            return _DEFAULT_PROPOSE_EVALS
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return _DEFAULT_PROPOSE_EVALS
    dream_table = data.get("loops", {}).get("dream", {}) if isinstance(data.get("loops"), dict) else {}
    value = dream_table.get("propose_evals") if isinstance(dream_table, dict) else None
    return value if isinstance(value, bool) else _DEFAULT_PROPOSE_EVALS


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    """No scanner jobs — the dream cron invokes the engine directly."""
    return []


MINI_LOOP = MiniLoop(
    name=DREAM_LOOP_NAME,
    default_cadence_seconds=DREAM_DEFAULT_CADENCE_SECONDS,
    build_jobs=_build_jobs,
    off_live_tick=True,
)
