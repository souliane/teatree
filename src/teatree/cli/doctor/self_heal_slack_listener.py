"""Self-heal detector for the Socket Mode listener heartbeat (owner directive #10).

Split from the sibling ``self_heal`` module: the heartbeat is written by another
container (the ``teatree-slack-listener`` service's WebSocket wait loop) into the
shared data mount, so reading and judging it is its own concern with its own file
format and staleness bound.

It measures the WEBSOCKET, not a sidecar: the stamp comes from inside a connected
overlay's own loop, so a dropped connection goes stale even while the container
keeps running. The predecessor watched a 15s ``t3 slack check`` sidecar, which
could report a healthy streak while the inbound socket was already dead.

Like every self-heal detector, :func:`check_slack_listener_alive` is crash-proof —
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
#: The listener heartbeat filename under :data:`teatree.paths.DATA_DIR` (the shared data
#: bind mount). ``teatree.backends.slack.receiver.write_heartbeat`` rewrites it from each
#: connected overlay loop; doctor — running in another container — reads it here. The
#: filename is pinned to the writer by ``tests/test_deploy_slack_listener.py``.
_HEARTBEAT_FILENAME = "slack-listener-heartbeat.json"
#: The heartbeat must refresh within max(this x its interval, floor) or the receiver
#: is dead/wedged. The multiplier absorbs a slow pass; the floor covers a fast cadence.
_STALE_MULTIPLIER = 4
_STALE_FLOOR_SECONDS = 120


@dataclass(frozen=True, slots=True)
class ListenerBeat:
    """One parsed listener heartbeat: when a connected loop last stamped it."""

    updated_at: dt.datetime
    interval_seconds: int


def read_heartbeat() -> "ListenerBeat | None":
    """The listener's last heartbeat, or ``None`` when absent/unreadable.

    ``None`` means the box runs no Socket Mode listener (a dev machine, or a deploy
    without one) OR the file is unparsable — the caller degrades to a pass, never a
    false FAIL. Read from :data:`teatree.paths.DATA_DIR` so a test can repoint the
    whole probe by patching that name on this module.
    """
    path = DATA_DIR / _HEARTBEAT_FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        updated = dt.datetime.fromtimestamp(int(raw["updated_at"]), tz=dt.UTC)
        return ListenerBeat(updated_at=updated, interval_seconds=int(raw.get("interval_seconds", 0)))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def check_slack_listener_alive() -> bool:
    """FAIL when the Socket Mode listener has gone silent.

    The ``teatree-slack-listener`` service holds one WebSocket per slack-enabled
    overlay and stamps this heartbeat from inside each connected loop. A stale stamp
    means every connection is gone (or the process died), so no inbound DM, mention
    or reaction is received at all — teatree goes silent rather than merely slow.
    Best-effort — an absent/unreadable heartbeat (no listener on this box) passes.
    """
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    try:
        beat = read_heartbeat()
        now = timezone.now()
    except Exception as exc:  # noqa: BLE001 — a self-heal probe must never crash the doctor run
        typer.echo(f"WARN  Slack-listener check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if beat is None:
        return True
    stale_after = max(_STALE_MULTIPLIER * beat.interval_seconds, _STALE_FLOOR_SECONDS)
    age = (now - beat.updated_at).total_seconds()
    if age > stale_after:
        typer.echo(
            f"FAIL  Slack-listener heartbeat is stale ({int(age)}s old, past {stale_after}s) — the "
            f"`teatree-slack-listener` WebSocket has dropped or the receiver has died, so NO inbound Slack "
            f"event is being received. Restart it: `docker compose -p {_COMPOSE_PROJECT} up -d "
            f"teatree-slack-listener`."
        )
        return False
    return True
