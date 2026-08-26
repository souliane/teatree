"""Resolving what opening a peer's loopback forward needs — the half that is not a host act.

The comparison page fetches each peer through a forward onto that box's own loopback-bound
dashboard, and nothing brought one up: the page rendered a command and left the operator to
paste it. A page that prints work for a human to do by hand is the workaround, so ``t3 peer
up`` opens it instead — and this module resolves what that takes.

Deliberately only the resolving half. Opening the forward is a HOST act: the transport
binaries and the credentials they use live on the operator's own machine, and the near end
has to bind the loopback the dashboard's fetch actually means, which a container's is not.
So the plan is written where :mod:`deploy/t3` reads it — the same handoff ``t3 admin`` uses
for its resolved url — and ``deploy/peer-forward.sh`` is the single place a forward is
opened, reused, refused or closed, whichever venue asked for it.

Nothing about any particular box is written down here. The peer's label, its host, its
transport's arguments and both ends of the forward all arrive from the ``peer_instances``
registry; this module knows only the shape of a plan, which is what keeps every private
coordinate in config and out of the source tree.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import teatree
from teatree.config import PeerInstance
from teatree.paths import data_dir_root
from teatree.utils.run import run_streamed

#: Where a forward's near end binds, and so where a browser reaches it: ``ssh -L`` binds
#: loopback unless ``GatewayPorts`` says otherwise, which nothing here asks it to.
NEAR_BIND = "127.0.0.1"

#: Where the CLI leaves the resolved plan for ``deploy/t3``. Under ``data_dir_root()``,
#: which is bind-mounted, so the host wrapper reads the bytes the container wrote.
PLAN_FILE = "peer-forward-plan"

#: The host-side runner, relative to the source root — the ONE place a forward is opened.
RUNNER = "deploy/peer-forward.sh"

#: How long to wait for a forward to start answering before reporting it did not come up.
#: A transport that must authenticate first takes seconds, not milliseconds.
DEFAULT_WAIT_SECONDS = 25.0


class ForwardAction(StrEnum):
    """What the host runner is being asked to do with each peer's forward."""

    UP = "up"
    DOWN = "down"
    STATUS = "status"


@dataclass(frozen=True, slots=True)
class ForwardPlan:
    """One peer's forward: the port it lands on, and the command that opens it."""

    peer: str
    port: int
    command: str

    def as_row(self) -> str:
        """The runner's row. ``command`` is LAST because it is a whole command line."""
        return f"{self.peer}\t{self.port}\t{self.command}"


def forward_plan(peer: PeerInstance, *, action: ForwardAction = ForwardAction.UP) -> ForwardPlan:
    """*peer*'s forward, or a refusal naming what its own registry entry does not say.

    Only the peer's label is ever echoed — the operator chose it, whereas the host it
    resolves to is exactly the coordinate that must not leave the registry.

    A tunnel is required to OPEN a forward and for nothing else. ``status`` reports whoever
    holds the port and ``down`` closes what teatree itself opened; neither reads the command.
    So a peer declaring no tunnel gets a row from those two rather than taking the whole run
    down with it — a peer in exactly the state ``t3 peer list`` prints as expected.

    The port is required for every action, because it is the port that is being reported on.
    """
    port = peer.local_port
    if port is None:
        message = f"peer {peer.name!r} has a url naming no port for its forward to land on"
        raise ValueError(message)
    if action is not ForwardAction.UP:
        return ForwardPlan(peer=peer.name, port=port, command="")
    if peer.tunnel is None:
        message = f"peer {peer.name!r} declares no tunnel, so there is no forward to open"
        raise ValueError(message)
    return ForwardPlan(peer=peer.name, port=port, command=peer.tunnel.command(port))


def plan_path() -> Path:
    return data_dir_root() / PLAN_FILE


def runner_path() -> Path:
    """The runner in the installed source tree, so the two are never two copies."""
    return Path(teatree.__file__).resolve().parents[2] / RUNNER


def write_plan(
    action: ForwardAction, plans: Sequence[ForwardPlan], *, wait_seconds: float = DEFAULT_WAIT_SECONDS
) -> Path:
    """Record *plans* where the host runner reads them, and answer where that is."""
    target = plan_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    header = f"action={action.value}\nwait_seconds={wait_seconds}\n"
    target.write_text(header + "".join(f"{plan.as_row()}\n" for plan in plans), encoding="utf-8")
    return target


def run_plan(action: ForwardAction, plans: Sequence[ForwardPlan], *, wait_seconds: float = DEFAULT_WAIT_SECONDS) -> int:
    """Record the plan and hand it to the runner — the path a host-native install takes."""
    plan_file = write_plan(action, plans, wait_seconds=wait_seconds)
    return run_streamed(["bash", str(runner_path()), str(plan_file)], check=False)


__all__ = [
    "DEFAULT_WAIT_SECONDS",
    "NEAR_BIND",
    "PLAN_FILE",
    "RUNNER",
    "ForwardAction",
    "ForwardPlan",
    "forward_plan",
    "plan_path",
    "run_plan",
    "runner_path",
    "write_plan",
]
