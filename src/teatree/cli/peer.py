"""``t3 peer`` (``list`` / ``up`` / ``down`` / ``status`` / ``open``) — the peers' loopback forwards.

The Compare Instances page reads each peer through a forward onto that box's own
loopback-bound dashboard. It rendered the command and stopped there, so bringing one up was
a paste-it-yourself step; these verbs are the missing execution.

Every coordinate comes from the ``peer_instances`` registry — the peer's label is the only
thing these commands ever echo, because the host it resolves to is precisely what must stay
in config. Resolution happens here; opening is a HOST act, so it goes to the one runner in
``deploy/peer-forward.sh`` — directly on a native install, and through ``deploy/t3``'s host
hop when this CLI is the containerized one, which can neither reach the transport's
credentials nor bind the loopback the fetch means.
"""

import webbrowser

import typer

from teatree.core.peer_forward import DEFAULT_WAIT_SECONDS, NEAR_BIND, ForwardAction, ForwardPlan, run_plan, write_plan

peer_app = typer.Typer(
    name="peer",
    no_args_is_help=True,
    help="The loopback forwards the Compare Instances page reads its peers through.",
)

_NAME_ARGUMENT = typer.Argument(None, help="Peer label from the registry; omit for every peer.")


def _peers(name: str | None, action: ForwardAction = ForwardAction.UP) -> list[ForwardPlan]:
    """Every named peer's forward for *action*, refusing the whole run rather than half of one.

    *action* is carried because what a peer's entry must say depends on it: only opening a
    forward needs a tunnel. Reporting on one, or closing it, reads the port and nothing else.
    """
    from teatree.config import load_peer_instances  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.core.peer_forward import forward_plan  # noqa: PLC0415 — deferred with its config sibling

    peers = load_peer_instances()
    if name is not None:
        peers = [peer for peer in peers if peer.name == name]
        if not peers:
            typer.echo(f"no peer named {name!r} is registered — `t3 peer list` shows the ones that are.", err=True)
            raise typer.Exit(code=1)
    plans = []
    for peer in peers:
        try:
            plans.append(forward_plan(peer, action=action))
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from None
    return plans


def _dispatch(action: ForwardAction, plans: list[ForwardPlan], *, wait_seconds: float) -> int:
    """Run the plan here, or leave it for the host hop that can — either way, its result."""
    from teatree.utils.ports import running_in_container  # noqa: PLC0415 — deferred: keeps CLI startup light

    if not running_in_container():
        return run_plan(action, plans, wait_seconds=wait_seconds)
    write_plan(action, plans, wait_seconds=wait_seconds)
    for plan in plans:
        typer.echo(f"{plan.peer}: {NEAR_BIND}:{plan.port}")
    return 0


@peer_app.command("list")
def list_peers() -> None:
    """Every registered peer, the port its forward lands on, and whether it declares one."""
    from teatree.config import load_peer_instances  # noqa: PLC0415 — deferred: keeps CLI startup light

    peers = load_peer_instances()
    if not peers:
        typer.echo("no peers are registered — set the `peer_instances` registry to compare against a box.")
        return
    for peer in peers:
        port = peer.local_port
        where = f"{NEAR_BIND}:{port}" if port else "no port in its url"
        declared = "a tunnel" if peer.tunnel else "NO tunnel — nothing can open its forward"
        typer.echo(f"{peer.name}: {where}, declares {declared}{f' — {peer.note}' if peer.note else ''}")


@peer_app.command("up")
def up(
    name: str | None = _NAME_ARGUMENT,
    *,
    wait_seconds: float = typer.Option(
        DEFAULT_WAIT_SECONDS, "--wait-seconds", help="How long to wait for the forward to start answering."
    ),
) -> None:
    """Open a peer's forward. A live one is reused; a foreign listener on its port is refused."""
    raise typer.Exit(code=_dispatch(ForwardAction.UP, _peers(name), wait_seconds=wait_seconds))


@peer_app.command("down")
def down(name: str | None = _NAME_ARGUMENT) -> None:
    """Close a forward teatree opened. One it did not open belongs to whoever did."""
    raise typer.Exit(
        code=_dispatch(ForwardAction.DOWN, _peers(name, ForwardAction.DOWN), wait_seconds=DEFAULT_WAIT_SECONDS)
    )


@peer_app.command("status")
def status(name: str | None = _NAME_ARGUMENT) -> None:
    """Whether each peer's forward answers, who holds its port, and whether teatree opened it."""
    raise typer.Exit(
        code=_dispatch(ForwardAction.STATUS, _peers(name, ForwardAction.STATUS), wait_seconds=DEFAULT_WAIT_SECONDS)
    )


@peer_app.command("open")
def open_peer(
    name: str = typer.Argument(..., help="Peer label from the registry."),
    *,
    admin: bool = typer.Option(False, "--admin", help="That peer's Django admin instead of its board."),
) -> None:
    """Bring that peer's forward up if it is not, then open its page in a browser on the host."""
    from teatree.cli.admin import ADMIN_PATH, BROWSE_URL_FILE, DASHBOARD_PATH  # noqa: PLC0415 — deferred: light startup
    from teatree.paths import data_dir_root  # noqa: PLC0415 — deferred with its sibling
    from teatree.utils.ports import running_in_container  # noqa: PLC0415 — deferred with its sibling

    plans = _peers(name)
    url = f"http://{NEAR_BIND}:{plans[0].port}{ADMIN_PATH if admin else DASHBOARD_PATH}"
    target = data_dir_root() / BROWSE_URL_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{url}\n", encoding="utf-8")
    code = _dispatch(ForwardAction.UP, plans, wait_seconds=DEFAULT_WAIT_SECONDS)
    if code != 0:
        raise typer.Exit(code=code)
    if not running_in_container():
        webbrowser.open(url)


__all__ = ["peer_app"]
