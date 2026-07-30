"""Self-heal detector for the slack-drain sidecar heartbeat (owner directive #10).

Split from the sibling ``self_heal`` module: the heartbeat is written by another
container (``deploy/entrypoint.sh``'s ``slack_drain_loop``) into the shared data
mount, so reading and judging it is its own concern with its own file format,
staleness bounds, and failure-streak threshold.

Like every self-heal detector, :func:`check_slack_drain_alive` is crash-proof —
any error degrades to a pass, since a detector that aborted the run would recreate
the "monitor dies, alerting dies" failure the module exists to end.
"""

import datetime as dt
import json
from dataclasses import dataclass

import typer

from teatree.paths import DATA_DIR

#: The compose project the box runs the factory under (``deploy/docker-compose.yml``).
_COMPOSE_PROJECT = "teatree"
#: The slack-drain sidecar heartbeat filename under :data:`teatree.paths.DATA_DIR`
#: (the shared data bind mount). ``deploy/entrypoint.sh``'s ``slack_drain_loop``
#: rewrites it every pass; doctor — running in another container — reads it here.
#: The filename is pinned to the entrypoint by ``tests/test_deploy_slack_listener.py``.
_HEARTBEAT_FILENAME = "slack-drain-heartbeat.json"
#: A drain that has failed this many passes in a row is a real, non-transient break.
_MAX_CONSECUTIVE_FAILURES = 5
#: The heartbeat must refresh within max(this x its interval, floor) or the sidecar
#: is dead/wedged. The multiplier absorbs a slow pass; the floor covers a fast cadence.
_STALE_MULTIPLIER = 4
_STALE_FLOOR_SECONDS = 120


@dataclass(frozen=True, slots=True)
class DrainBeat:
    """One parsed slack-drain heartbeat: when it last ran and its failure streak."""

    updated_at: dt.datetime
    consecutive_failures: int
    interval_seconds: int


def read_heartbeat() -> "DrainBeat | None":
    """The slack-drain sidecar's last heartbeat, or ``None`` when absent/unreadable.

    ``None`` means the box runs no slack-drain sidecar (a dev machine, or a
    deploy without the listener) OR the file is unparsable — the caller
    degrades to a pass, never a false FAIL. Read from
    :data:`teatree.paths.DATA_DIR` so a test can repoint the whole probe by
    patching that name on this module.
    """
    path = DATA_DIR / _HEARTBEAT_FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        updated = dt.datetime.fromtimestamp(int(raw["updated_at"]), tz=dt.UTC)
        return DrainBeat(
            updated_at=updated,
            consecutive_failures=int(raw["consecutive_failures"]),
            interval_seconds=int(raw.get("interval_seconds", 0)),
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def check_slack_drain_alive() -> bool:
    """FAIL when the slack-drain sidecar is failing every pass or has gone silent.

    The ``teatree-slack-listener`` service drains inbound Slack every ~15s
    (``deploy/entrypoint.sh`` ``slack_drain_loop``) and rewrites a heartbeat with
    its consecutive-failure count. A drain failing pass after pass (``t3 slack
    check`` erroring — Django won't boot, DB unreachable) or a heartbeat gone
    stale (the loop died or hung) both mean captured DMs never reach the answer
    pipeline: teatree reacts 👀 but silently stops answering. Best-effort — an
    absent/unreadable heartbeat (no sidecar on this box) degrades to a pass.
    """
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    try:
        beat = read_heartbeat()
        now = timezone.now()
    except Exception as exc:  # noqa: BLE001 — a self-heal probe must never crash the doctor run
        typer.echo(f"WARN  Slack-drain check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if beat is None:
        return True
    stale_after = max(_STALE_MULTIPLIER * beat.interval_seconds, _STALE_FLOOR_SECONDS)
    age = (now - beat.updated_at).total_seconds()
    if age > stale_after:
        typer.echo(
            f"FAIL  Slack-drain heartbeat is stale ({int(age)}s old, past {stale_after}s) — the "
            f"`teatree-slack-listener` drain loop has died or hung, so inbound Slack is no longer drained "
            f"or answered. Restart it: `docker compose -p {_COMPOSE_PROJECT} up -d teatree-slack-listener`."
        )
        return False
    if beat.consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
        typer.echo(
            f"FAIL  Slack drain has failed {beat.consecutive_failures} passes in a row — `t3 slack check` "
            f"keeps erroring in the `teatree-slack-listener` sidecar, so captured DMs never get 👀-acked or "
            f"answered. Inspect `docker compose -p {_COMPOSE_PROJECT} logs teatree-slack-listener`."
        )
        return False
    return True
