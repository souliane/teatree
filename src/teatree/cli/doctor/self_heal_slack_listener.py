"""Self-heal detector for the Socket Mode listener heartbeat (owner directive #10).

Split from the sibling ``self_heal`` module: the heartbeat is written by another
container (the ``teatree-slack-listener`` service's WebSocket wait loop) into the
shared data mount, so reading and judging it is its own concern. The file format and
staleness bound live in :mod:`teatree.core.slack_listener_heartbeat`; the writer is beside its connection.

It measures each overlay's WEBSOCKET, not a sidecar: the stamp comes from inside a
connected overlay's own loop, so a dropped connection goes stale even while the
container keeps running.

Like every self-heal detector, :func:`check_slack_listener_alive` is crash-proof —
any error degrades to a pass, since a detector that aborted the run would recreate
the "monitor dies, alerting dies" failure the module exists to end.
"""

import time

import typer

from teatree.cli.slack.app_resolve import read_overlay_registry
from teatree.core.slack_listener_heartbeat import heartbeat_is_stale, read_heartbeat

#: The compose project the box runs the factory under (``deploy/docker-compose.yml``).
_COMPOSE_PROJECT = "teatree"


def _slack_overlays() -> list[str]:
    return sorted(
        name
        for name, block in read_overlay_registry().items()
        if isinstance(block, dict) and block.get("messaging_backend") == "slack"
    )


def check_slack_listener_alive() -> bool:
    """FAIL when a configured Slack overlay's Socket Mode connection has gone silent.

    The ``teatree-slack-listener`` service holds one WebSocket per slack-enabled
    overlay and stamps that overlay in the heartbeat from inside its connected loop.
    An absent file or a stale stamp means no live event reaches that overlay; the
    inbox catch-up job then recovers its messages once a minute instead of in real
    time. A box with no Slack overlay passes.
    """
    try:
        overlays = _slack_overlays()
        beat = read_heartbeat()
        now = time.time()
    except Exception as exc:  # noqa: BLE001 — a self-heal probe must never crash the doctor run
        typer.echo(f"WARN  Slack-listener check crashed: {exc.__class__.__name__}: {exc}")
        return True
    dead = [name for name in overlays if heartbeat_is_stale(beat, name, now=now)]
    for name in dead:
        stamp = beat.stamp_for(name) if beat is not None else None
        state = "has no heartbeat" if beat is None or stamp is None else f"heartbeat is stale ({int(now - stamp)}s old)"
        typer.echo(
            f"FAIL  Slack listener for {name} {state} — real-time Slack delivery is down and the inbox "
            f"catch-up job is recovering messages once a minute. Restart it: `docker compose -p "
            f"{_COMPOSE_PROJECT} up -d teatree-slack-listener`."
        )
    return not dead
