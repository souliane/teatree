"""Obtaining each instance's settings snapshot — fetched from a peer, or loaded from a file.

A comparison column is one box's answer, and there are two ways to get one. A peer is
FETCHED over a loopback tunnel to its own loopback-bound dashboard (``ssh -L``), so nothing
here asks any instance to publish a port; the snapshot PATH is derived from the URLconf
rather than written out, so the two ends cannot drift. A record is LOADED from a snapshot
file the operator already has (:mod:`teatree.dash.settings_files`), which is the only route
to a box whose tunnel is down, a box that is gone, and this box as it stood weeks ago.

Both routes produce the same :class:`PeerSnapshot`, so the comparison has one set of rules
rather than two. :attr:`PeerSnapshot.origin` is what keeps them distinguishable where it
matters: a loaded column is labelled as a record and carries its own ``captured_at``, and a
record never decides whether the LIVE boxes may be compared.

Every configured peer appears in the result — one that could not be reached comes back as a
row carrying its REASON, never as a gap. A silently dropped peer is the failure this module
exists to prevent: a comparison page missing one box reads as agreement between the boxes
that did answer.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from django.urls import reverse

from teatree.config import PeerInstance, load_peer_instances
from teatree.core.settings_snapshot import SNAPSHOT_FORMAT, build_snapshot
from teatree.utils.ports import host_published_port_host

logger = logging.getLogger(__name__)

#: What this instance calls itself in a comparison it serves.
LOCAL_LABEL = "this instance"

#: The names a peer's url uses for the machine its forward is opened on.
_LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})


class SnapshotOrigin(StrEnum):
    """Whether a column is a reading taken NOW or a record of some earlier moment."""

    LIVE = "live"
    FILE = "file"


@dataclass(frozen=True, slots=True)
class PeerSnapshot:
    """One instance's answer — its payload, or why there is none."""

    label: str
    url: str
    note: str
    payload: Mapping[str, Any] | None = None
    error: str = ""
    origin: SnapshotOrigin = SnapshotOrigin.LIVE
    #: Where a LOADED record came from; a fetched peer states its url instead.
    source: str = ""
    #: What brings this peer's tunnel up, rendered from the registry's own tunnel fields.
    #: Empty where the peer declares no tunnel — the page then says nothing rather than
    #: offering a command that cannot succeed.
    tunnel_command: str = ""

    @property
    def reachable(self) -> bool:
        return self.payload is not None

    @property
    def from_file(self) -> bool:
        return self.origin is SnapshotOrigin.FILE

    @property
    def provenance(self) -> str:
        """Where this column's payload came from, as the instance band states it."""
        return self.url or self.source or "this process"

    @property
    def captured_at(self) -> str:
        """When the payload was taken — a record's age, and the reason it can never read live."""
        return str((self.payload or {}).get("captured_at", ""))

    @property
    def fingerprint(self) -> Mapping[str, Any]:
        return _mapping(self.payload, "fingerprint")

    @property
    def instance(self) -> Mapping[str, Any]:
        return _mapping(self.payload, "instance")

    @property
    def registry(self) -> Mapping[str, Any]:
        return _mapping(self.payload, "registry")

    @property
    def values(self) -> Mapping[str, Any]:
        return _mapping(self.payload, "values")


def local_snapshot() -> PeerSnapshot:
    """This instance's own snapshot — the left-hand column every peer is compared against."""
    try:
        return PeerSnapshot(label=LOCAL_LABEL, url="", note="", payload=build_snapshot(LOCAL_LABEL))
    except Exception as exc:
        logger.warning("the local settings snapshot could not be built", exc_info=True)
        return PeerSnapshot(label=LOCAL_LABEL, url="", note="", error=f"{type(exc).__name__}: {exc}")


def peer_snapshots() -> tuple[PeerSnapshot, ...]:
    """Every configured peer, fetched — an unreachable one carries its reason, never a gap."""
    try:
        peers = load_peer_instances()
    except Exception as exc:
        logger.warning("the peer_instances registry could not be read", exc_info=True)
        return (PeerSnapshot(label="peer_instances", url="", note="", error=f"{type(exc).__name__}: {exc}"),)
    return tuple(_snapshot_of(peer) for peer in peers)


def _snapshot_of(peer: PeerInstance) -> PeerSnapshot:
    """One peer's row, whatever its own fields turn out to be.

    Every read of *peer* happens under this guard, ``tunnel_command`` included: it derives the
    forward's port from the peer's url, so a url naming no port used to raise from OUTSIDE any
    guard and 500 the page — losing the good peers along with the typo'd one.
    """
    try:
        return _fetch(peer.name, peer.url, peer.note, peer.timeout_seconds, peer.tunnel_command)
    except Exception as exc:
        logger.warning("peer %r could not be resolved from the registry", peer.name, exc_info=True)
        return PeerSnapshot(label=peer.name, url="", note="", error=f"{type(exc).__name__}: {exc}")


def snapshot_url(base: str) -> str:
    """*base* joined with the snapshot route as the URLconf declares it."""
    return urljoin(base if base.endswith("/") else f"{base}/", reverse("dash:settings_snapshot").lstrip("/"))


@dataclass(frozen=True, slots=True)
class FetchTarget:
    """Where to connect, and the name to ask for once connected — not always the same one."""

    url: str
    headers: Mapping[str, str] = field(default_factory=dict)


def fetch_target(url: str) -> FetchTarget:
    """*url* as it resolves from the process doing the fetching.

    A peer's url names the near end of a forward, bound on the machine holding the credentials
    that opened it. Under the deploy stack the dashboard runs in a container, whose own
    ``127.0.0.1`` has nothing on it — so every peer would read as refused while its forward is
    demonstrably up. Resolved, never configured: the venue is this process's to know.

    The peer is still ASKED by the name it answers to. Its ``ALLOWED_HOSTS`` knows the loopback
    it binds and nothing else, so carrying the route's name in the Host header turns the refusal
    into a 400 — a peer that reads as broken because of a header this side rewrote.
    """
    split = urlsplit(url)
    host = host_published_port_host()
    if split.hostname not in _LOOPBACK_NAMES or host in _LOOPBACK_NAMES:
        return FetchTarget(url=url)
    routed = split._replace(netloc=f"{host}:{split.port}" if split.port else host)
    return FetchTarget(url=urlunsplit(routed), headers={"Host": split.netloc})


def _fetch(label: str, base: str, note: str, timeout: float, tunnel_command: str = "") -> PeerSnapshot:
    # The tunnel command rides along on every outcome, because the row that needs it most is
    # the one that failed: a refused connection means the forward is not up, and the operator
    # reading that row is exactly who has to bring it up.
    if not base:
        return PeerSnapshot(label=label, url="", note=note, error="no url configured for this peer")
    url = snapshot_url(base)
    try:
        target = fetch_target(url)
        response = httpx.get(target.url, timeout=timeout, follow_redirects=True, headers=target.headers)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.info("peer %r could not be fetched from %s", label, url, exc_info=True)
        return PeerSnapshot(
            label=label, url=url, note=note, error=f"{type(exc).__name__}: {exc}", tunnel_command=tunnel_command
        )
    if not isinstance(payload, dict) or payload.get("format") != SNAPSHOT_FORMAT:
        return PeerSnapshot(
            label=label,
            url=url,
            note=note,
            error=f"that url did not answer a {SNAPSHOT_FORMAT}",
            tunnel_command=tunnel_command,
        )
    return PeerSnapshot(label=label, url=url, note=note, payload=payload, tunnel_command=tunnel_command)


def _mapping(payload: Mapping[str, Any] | None, key: str) -> Mapping[str, Any]:
    value = (payload or {}).get(key)
    return value if isinstance(value, Mapping) else {}


__all__ = [
    "LOCAL_LABEL",
    "FetchTarget",
    "PeerSnapshot",
    "SnapshotOrigin",
    "fetch_target",
    "local_snapshot",
    "peer_snapshots",
    "snapshot_url",
]
