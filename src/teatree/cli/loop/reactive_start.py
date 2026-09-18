"""Shared guard for the three ``t3 loop <slot> start`` commands (#2663).

Each reactive slot now runs as a worker chain, so handing an operator a paste-me
``/loop`` while a worker holds the singleton buys nothing: every tick of the
registered loop stands down against that worker. The three ``start`` commands
therefore say so instead of printing a registration nobody should act on.
"""

import typer


def _a_worker_is_running() -> bool:
    from teatree.loop.queue_drain import a_worker_is_running  # noqa: PLC0415 (deferred: no Django/DB at CLI import)

    return a_worker_is_running()


def _worker_is_running() -> bool:
    """Whether a worker owns the cadence; an unprovable probe reports False (print the registration)."""
    try:
        return _a_worker_is_running()
    except Exception:  # noqa: BLE001 — an unreadable singleton must not withhold the operator's registration.
        return False


def worker_already_drives(slot_label: str) -> bool:
    """Report that the worker drives *slot_label* and return True, or return False to print the slot."""
    if not _worker_is_running():
        return False
    typer.echo(f"A live `t3 worker` already drives the {slot_label} cycle as a worker chain — nothing to register.")
    typer.echo("Confirm with `t3 worker status`. Register this slot only on a box whose worker is down.")
    return True


__all__ = ["worker_already_drives"]
