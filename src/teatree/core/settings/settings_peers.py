"""Local and imported settings snapshots, with configured peers shown as import prompts."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from teatree.config import PeerInstance, load_peer_instances
from teatree.core.settings_snapshot import build_snapshot

logger = logging.getLogger(__name__)

#: What this instance calls itself in a comparison it serves.
LOCAL_LABEL = "this instance"


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
    #: Where a loaded record came from; a configured peer states its URL instead.
    source: str = ""

    @property
    def reachable(self) -> bool:
        return self.payload is not None

    @property
    def from_file(self) -> bool:
        return self.origin is SnapshotOrigin.FILE

    @property
    def local(self) -> bool:
        """Whether this column is THIS process — the only box the page may offer to write."""
        return self.origin is SnapshotOrigin.LIVE and not self.url

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
    """Show configured peers without making unauthenticated dashboard requests."""
    try:
        peers = load_peer_instances()
    except Exception as exc:
        logger.warning("the peer_instances registry could not be read", exc_info=True)
        return (PeerSnapshot(label="peer_instances", url="", note="", error=f"{type(exc).__name__}: {exc}"),)
    return tuple(_snapshot_of(peer) for peer in peers)


def _snapshot_of(peer: PeerInstance) -> PeerSnapshot:
    try:
        return PeerSnapshot(label=peer.name, url=peer.url, note=peer.note, error="import a saved snapshot to compare")
    except Exception as exc:
        logger.warning("peer %r could not be resolved from the registry", peer.name, exc_info=True)
        return PeerSnapshot(label=peer.name, url="", note="", error=f"{type(exc).__name__}: {exc}")


def _mapping(payload: Mapping[str, Any] | None, key: str) -> Mapping[str, Any]:
    value = (payload or {}).get(key)
    return value if isinstance(value, Mapping) else {}


__all__ = [
    "LOCAL_LABEL",
    "PeerSnapshot",
    "SnapshotOrigin",
    "local_snapshot",
    "peer_snapshots",
]
