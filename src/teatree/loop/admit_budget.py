"""Orchestrate admit-budget sidecar — the read-only fan-out ceiling (#1796).

The reconciled ``wip=full`` fan-out (#1796) keeps exactly ONE mutation
point: the ``claim_next_pending`` compare-and-swap (the #786 boundary). The
``orchestrate_phase`` planner no longer claims; it computes a per-tick admit
*budget* (the clamped fan-out cap) and the tick persists it here, beside the
``tick-meta.json`` freshness sidecar (the established cross-process channel —
same shape as ``open-prs.json``). The live claimer (``loop_dispatch
claim-next``) reads it and refuses once the standing in-flight claimed WIP
hits the ceiling, so claimed ≡ spawned and the orphan window is eliminated.

Two halves cross the process boundary through one JSON file:

*   :func:`write_admit_budget` (tick side) — clamps wip → cap, writes the
    ``orchestrate_admit_budget`` key + a ``…_written_at`` epoch. Last-writer-
    wins, idempotent per tick.
*   :func:`read_admit_budget` (claimer side) — returns the budget only when it
    is fresh (``written_at`` within ~2x the tick cadence); **fails open to
    UNCLAMPED** (returns ``None``) on absence, a missing/stale timestamp, a
    corrupt sidecar, or a non-int value. A dead loop's stale budget must never
    wrongly throttle live dispatch — absence is today's unclamped throughput.

At ``medium`` wip or with the toggle off the tick writes NO budget key (it
clears any prior one), so the claimer reads ``None`` = unclamped = byte-
identical to today.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The ``tick-meta.json`` sidecar is a free-form JSON object (freshness header,
#: cost chip, next-epoch, plus this module's budget keys) — an arbitrary JSON
#: mapping by contract, the same shape ``teatree.loop.dispatch`` types its
#: payloads with.
type TickMeta = dict[str, Any]

#: The sidecar JSON key carrying the per-tick admit ceiling. Absence means
#: "no clamp this tick" (unclamped — today's throughput).
BUDGET_KEY = "orchestrate_admit_budget"

#: Companion key: the epoch the budget was written, for the freshness TTL.
WRITTEN_AT_KEY = f"{BUDGET_KEY}_written_at"

#: A budget older than ``_TTL_CADENCE_MULTIPLIER`` * the tick cadence is
#: presumed written by a now-dead loop and is ignored (fail open to unclamped).
_TTL_CADENCE_MULTIPLIER = 2


def _meta_path(statusline_path: Path) -> Path:
    return statusline_path.with_name("tick-meta.json")


def _load_meta(meta_path: Path) -> TickMeta:
    try:
        body = meta_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def write_admit_budget(budget: int, *, statusline_path: Path) -> None:
    """Persist the per-tick admit ceiling, merging into the tick-meta sidecar.

    Reads the existing ``tick-meta.json`` (freshness, cost chip, next-epoch),
    merges in the budget key + the ``written_at`` timestamp, and writes it
    back — so the budget and the freshness header share one file without one
    clobbering the other. The parent dir is ensured (mirrors
    ``_write_tick_meta`` / ``write_open_prs_cache``) so an observability write
    can never crash the tick.
    """
    meta_path = _meta_path(statusline_path)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _load_meta(meta_path)
    payload[BUDGET_KEY] = int(budget)
    payload[WRITTEN_AT_KEY] = time.time()
    meta_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def clear_admit_budget(*, statusline_path: Path) -> None:
    """Remove the budget key (medium / toggle-off path) — absence = unclamped.

    Surgical: drops only the budget + ``written_at`` keys, preserving every
    other tick-meta key. A no-op when the sidecar (or the key) is absent.
    """
    meta_path = _meta_path(statusline_path)
    payload = _load_meta(meta_path)
    if BUDGET_KEY not in payload and WRITTEN_AT_KEY not in payload:
        return
    payload.pop(BUDGET_KEY, None)
    payload.pop(WRITTEN_AT_KEY, None)
    try:
        meta_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    except OSError:
        logger.exception("clear_admit_budget could not rewrite %s", meta_path)


def read_admit_budget(*, statusline_path: Path, cadence_seconds: int) -> int | None:
    """Return the fresh admit budget, or ``None`` (UNCLAMPED) when unusable.

    Fails open to ``None`` — the unclamped, today's-throughput answer — when:

    *   the sidecar is absent or corrupt,
    *   the budget key is missing or not an ``int``,
    *   the ``written_at`` timestamp is missing (cannot prove freshness),
    *   the budget is older than ``2 * cadence`` (a dead loop wrote it).

    A dead loop must never wrongly clamp live dispatch, so every uncertain
    case degrades to ``None`` (no clamp), never to a residual stale ceiling.
    """
    payload = _load_meta(_meta_path(statusline_path))
    raw_budget = payload.get(BUDGET_KEY)
    if not isinstance(raw_budget, int) or isinstance(raw_budget, bool):
        return None
    written_at = payload.get(WRITTEN_AT_KEY)
    if not isinstance(written_at, (int, float)) or isinstance(written_at, bool):
        return None
    ttl = _TTL_CADENCE_MULTIPLIER * max(60, int(cadence_seconds))
    if time.time() - float(written_at) > ttl:
        return None
    return raw_budget


__all__ = [
    "BUDGET_KEY",
    "WRITTEN_AT_KEY",
    "clear_admit_budget",
    "read_admit_budget",
    "write_admit_budget",
]
