"""Continuous stop-snapshotter — hook adapters (souliane/teatree#2564, PR-20).

Two thin adapters onto :func:`teatree.core.stop_snapshot.prepare_stop`, the
shared recovery-snapshot implementation:

- :func:`handle_stop_snapshot_slot` — the always-on 5-minute Stop-event slot.
    The Stop hook fires every turn regardless of loop pause / availability, so
    a per-session throttle marker turns it into a cadence: every ~5 minutes it
    refreshes the durable recovery artifacts, even while loops are paused. It is
    pure infra — no colleague-facing post/react — and never blocks the turn.
- :func:`run_prepare_stop_best_effort` — the ``PreCompact`` compaction adapter.
    Compaction should always refresh the snapshot, so this runs unthrottled.

Both bootstrap Django lazily (only when there is work to do) and swallow every
error: a snapshot must never block a Stop or a compaction (#845 / #970
invariant). :func:`open_prs_for_repo` is the gh-based open-PR reader the
PreCompact durable snapshot renders — extracted here from ``hook_router`` so the
dispatcher shrinks; the router re-exports it unchanged.

Cold-import safe: the module top imports only stdlib, so the live hook (a bare
``python3`` subprocess with no guarantee ``teatree`` is importable) loads it
without a Django bootstrap; Django and ``teatree.core`` are reached only lazily.
"""

import json
import os
import subprocess  # noqa: S404 — fixed-argv gh reader, no shell
import sys
import time
from pathlib import Path

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# re-exports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("stop_snapshot_slot", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.stop_snapshot_slot", sys.modules[__name__])

_SLOT_INTERVAL_SECONDS = 300  # the always-on 5-minute cadence
_MARKER_PREFIX = "t3-stop-snapshot-"


def open_prs_for_repo(repo_path: Path) -> list[dict]:
    """Return open PRs authored by the current user for *repo_path*.

    Best-effort: a missing ``gh``, no auth, no network, or a non-GitHub
    remote returns ``[]``. Never raises.
    """
    if not (repo_path / ".git").exists():
        return []
    try:
        out = subprocess.check_output(
            [  # noqa: S607 — gh on PATH; fixed argv, no shell
                "gh",
                "pr",
                "list",
                "--author",
                "@me",
                "--state",
                "open",
                "--limit",
                "20",
                "--json",
                "number,title,headRefName,isDraft",
            ],
            cwd=str(repo_path),
            text=True,
            timeout=3,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _state_dir() -> Path:
    """Mirror ``hook_router.STATE_DIR`` without importing it (avoids a cycle)."""
    return Path(
        os.environ.get(
            "TEATREE_CLAUDE_STATUSLINE_STATE_DIR",
            os.environ.get("T3_HOOK_STATE_DIR", "/tmp/claude-statusline"),  # noqa: S108 — mirrors STATE_DIR default
        )
    )


def _slot_marker(session_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in session_id) or "unknown"
    return _state_dir() / f"{_MARKER_PREFIX}{safe}.stamp"


def _slot_due(marker: Path, *, now: float) -> bool:
    """Whether ``_SLOT_INTERVAL_SECONDS`` have elapsed since the last run."""
    try:
        return (now - marker.stat().st_mtime) >= _SLOT_INTERVAL_SECONDS
    except OSError:
        return True  # no marker yet → first run is due


def _claim_slot(marker: Path) -> None:
    """Stamp the marker now so a failing run does not retry every turn."""
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def _slot_enabled() -> bool:
    """Kill-switch — ``[teatree] stop_snapshotter_enabled = false`` disables it.

    Fails OPEN to enabled (always-on infra) on a missing/broken config.
    """
    try:
        from teatree_settings import teatree_bool_setting  # noqa: PLC0415 — lazy cold-import

        return teatree_bool_setting("stop_snapshotter_enabled", default=True)
    except Exception:  # noqa: BLE001 — a config read must never wedge the slot
        return True


def _run_prepare_stop(session_id: str, data: dict) -> None:
    """Bootstrap Django and refresh the recovery artifacts — best-effort, silent."""
    try:
        from django_bootstrap import bootstrap_teatree_django  # noqa: PLC0415 — lazy cold-import

        if not bootstrap_teatree_django():
            return
        from teatree.core.stop_snapshot import prepare_stop  # noqa: PLC0415 — lazy cold-import

        prepare_stop(session_id, data.get("cwd", "") or "")
    except Exception:  # noqa: BLE001 — a snapshot must never block a Stop / compaction
        return


def run_prepare_stop_best_effort(session_id: str, data: dict) -> None:
    """PreCompact adapter — refresh the recovery artifacts unthrottled."""
    _run_prepare_stop(session_id, data)


def handle_stop_snapshot_slot(data: dict) -> None:
    """Always-on 5-minute Stop-event snapshot slot (runs even while paused).

    Fast path is Django-free: a not-due throttle check returns immediately.
    When due, it claims the slot (stamps the marker first, so a failure does
    not retry every turn) and refreshes the recovery artifacts. Returns
    ``None`` always — pure infra, never a Stop-block verdict.
    """
    if not _slot_enabled():
        return
    session_id = data.get("session_id", "")
    marker = _slot_marker(session_id)
    if not _slot_due(marker, now=time.time()):
        return
    _claim_slot(marker)
    _run_prepare_stop(session_id, data)
    return
