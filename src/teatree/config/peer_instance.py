"""The ``PeerInstance`` value object — another teatree box this one can be compared against.

A sibling of :mod:`teatree.config.e2e_repo`: a standalone value object with no dependency on
the settings machinery, so the registry loader can build it without importing settings
resolution. Re-exported from :mod:`teatree.config`.

The ``url`` is a LOOPBACK TUNNEL address (an ``ssh -L`` forward to the peer's own
loopback-bound dashboard), never a published port — the peer's dashboard stays loopback-only
and the tunnel is what makes it reachable from here.

Which leaves the question this module answers: a url alone says where the forward LANDS, not
how to bring it up. A registry entry's key is a LABEL — what the comparison calls that box —
and a label is not a connectable target. An operator names their boxes for the comparison,
not for ssh, so those names routinely resolve as no host at all. So a peer that wants its
tunnel command shown declares a :class:`PeerTunnel` saying where to connect and by which
transport, and the command is RENDERED from that plus the peer's own url. Two consequences,
both deliberate:

*   the forward's local port is read off the url rather than typed a second time, so the
    printed command cannot drift from the address it is supposed to open; and
*   a box reached some way other than plain ssh — a GCE instance with no public IP, reachable
    only through IAP — states its transport and gets the command that actually works.

A peer that declares no tunnel shows no command. That is the honest answer: guessing
``ssh <label>`` is what printed an unrunnable command in the first place, and a command that
cannot succeed is worse than none.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit

#: How long a peer fetch waits before it is reported as an unreachable peer rather than hanging
#: the comparison page. A tunnel that is down fails fast; a live one answers in milliseconds.
DEFAULT_TIMEOUT_SECONDS = 5.0

#: The port a teatree dashboard binds on its own box's loopback — what the forward reaches.
DEFAULT_REMOTE_PORT = 8000

#: What the far end of the forward resolves ON THE PEER, which is the peer's own loopback.
REMOTE_BIND = "127.0.0.1"

#: What every transport's ssh needs, whichever brokered it. ``ExitOnForwardFailure`` is what
#: makes a forward that cannot bind FAIL rather than sit there connected and forwarding nothing.
FORWARD_ARGUMENTS = ("-N", "-o", "ExitOnForwardFailure=yes")


class PeerTransport(StrEnum):
    """How the forward is opened — the peers that exist differ in this, not in the forward."""

    #: ``ssh -N -L …`` straight to a reachable host (a hostname, an ip, or an ssh-config alias).
    SSH = "ssh"
    #: ``gcloud compute ssh … --tunnel-through-iap`` for a GCE instance with no public IP,
    #: where plain ssh has nothing to connect to and the command shape differs, not just the host.
    GCLOUD_IAP = "gcloud-iap"


@dataclass(frozen=True, slots=True)
class PeerTunnel:
    """Where to connect, and how, to bring up one peer's loopback forward."""

    #: The connectable target — an ssh destination, or the instance name for ``gcloud-iap``.
    #: Distinct from the registry key on purpose: that key is the label shown in the comparison.
    host: str
    transport: PeerTransport = PeerTransport.SSH
    remote_port: int = DEFAULT_REMOTE_PORT
    #: Extra arguments the transport needs, verbatim — ``-i``/``-p``/``-J`` for ssh,
    #: ``--project``/``--zone`` for gcloud.
    options: tuple[str, ...] = ()

    def argv(self, local_port: int) -> tuple[str, ...]:
        """The argv that opens *local_port* onto this peer's dashboard — the executed form.

        ``host`` and ``options`` come verbatim out of the ``peer_instances`` row, and the
        runner executes this on the operator's own machine, outside the container. As one
        joined STRING handed to ``sh -c`` a host of ``box; curl … | sh`` ran as the operator;
        as argv it is one token that names no host and nothing else.

        No ``-f``: the forward stays in the foreground so the process that opened it is the one
        the runner tracks, and can therefore close.
        """
        forward = [*FORWARD_ARGUMENTS, "-L", f"{local_port}:{REMOTE_BIND}:{self.remote_port}"]
        if self.transport is PeerTransport.GCLOUD_IAP:
            head = ["gcloud", "compute", "ssh", self.host, *self.options, "--tunnel-through-iap", "--"]
            return (*head, *forward)
        return ("ssh", *self.options, *forward, self.host)

    def command(self, local_port: int) -> str:
        """:meth:`argv` rendered for a human to read or paste — never for execution.

        Joined plainly rather than shell-quoted so a ``~/.ssh/...`` option stays something the
        operator's own shell expands. That is safe HERE and only here: nothing runs this.
        """
        return " ".join(self.argv(local_port))


@dataclass(frozen=True, slots=True)
class PeerInstance:
    """One peer teatree instance: its label in the comparison and where to fetch it from."""

    name: str
    url: str
    note: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    tunnel: PeerTunnel | None = field(default=None)

    @property
    def local_port(self) -> int | None:
        """The forward's near end, read off this peer's own url so the two cannot disagree.

        A url naming no port a forward could land on is ``None`` rather than a raise: this is
        read for EVERY peer while rendering the comparison, so one typo here escaping would
        take the whole page down — including the peers that are perfectly well configured.
        """
        try:
            return urlsplit(self.url).port
        except ValueError:
            return None

    @property
    def tunnel_command(self) -> str:
        """The command that brings this peer's tunnel up, or ``""`` when it declares none."""
        port = self.local_port
        if self.tunnel is None or port is None:
            return ""
        return self.tunnel.command(port)


__all__ = [
    "DEFAULT_REMOTE_PORT",
    "DEFAULT_TIMEOUT_SECONDS",
    "FORWARD_ARGUMENTS",
    "REMOTE_BIND",
    "PeerInstance",
    "PeerTransport",
    "PeerTunnel",
]
