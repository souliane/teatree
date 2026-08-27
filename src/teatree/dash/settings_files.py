"""A settings snapshot as a FILE — the name one is saved under, and the load of one back in.

The comparison page reaches a peer over a loopback tunnel, which needs that peer to be UP
right now. A snapshot is also a RECORD, and the three columns a live fetch can never produce
are exactly the ones a saved file can: a box whose tunnel is not up, a box that is gone, and
THIS box as it stood at some earlier date. So a loaded file joins the same comparison as one
more column under the same rules — it is not a second mode with its own answers.

A loaded column says it came from a file and carries the snapshot's own ``captured_at``, so a
record can never be mistaken for a live reading. A document that is not a
:data:`~teatree.core.settings_snapshot.SNAPSHOT_FORMAT` payload, or carries a
``format_version`` this teatree does not read, is refused BY NAME, saying which document and
what was wrong with it: a file dropped in silence reads as a box that agrees with everything.

**Loading writes nothing.** A load parses bytes into an in-memory
:class:`~teatree.dash.settings_peers.PeerSnapshot` for the one request that carried it — no
setting, no row and no file on this box is touched, and nothing survives the response.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import starmap
from typing import Any

from django.utils.text import slugify

from teatree.core.settings_snapshot import FORMAT_VERSION, SNAPSHOT_FORMAT
from teatree.dash.settings_peers import PeerSnapshot, SnapshotOrigin

#: The largest document the loader accepts. A real snapshot measures a few hundred KB, so this
#: is generous for any capture while refusing a mis-picked archive before it reaches the parser.
MAX_SNAPSHOT_BYTES = 8_000_000

#: How much of an ISO ``captured_at`` names the day — the granularity a filename wants.
_DATE_CHARS = 10


@dataclass(frozen=True, slots=True)
class LoadRefusal:
    """One document that could not be loaded, and what was wrong with it."""

    source: str
    reason: str


@dataclass(frozen=True, slots=True)
class LoadedSnapshots:
    """What a load attempt produced: the columns it added, and every document it refused."""

    snapshots: tuple[PeerSnapshot, ...] = ()
    refusals: tuple[LoadRefusal, ...] = ()

    @property
    def attempted(self) -> bool:
        return bool(self.snapshots or self.refusals)


def load_snapshots(documents: Sequence[tuple[str, bytes]]) -> LoadedSnapshots:
    """Load every *(source, raw)* document — each one becomes a column or a named refusal."""
    results = list(starmap(_load_one, documents))
    return LoadedSnapshots(
        snapshots=tuple(one for one in results if isinstance(one, PeerSnapshot)),
        refusals=tuple(one for one in results if isinstance(one, LoadRefusal)),
    )


def snapshot_filename(payload: Mapping[str, Any]) -> str:
    """The name a saved snapshot lands under — the instance's own label and its capture date."""
    label = slugify(_instance_field(payload, "label")) or "instance"
    day = slugify(str(payload.get("captured_at", ""))[:_DATE_CHARS]) or "undated"
    return f"teatree-settings-{label}-{day}.json"


def _load_one(source: str, raw: bytes) -> PeerSnapshot | LoadRefusal:
    payload, refusal = _snapshot_document(raw)
    if refusal:
        return LoadRefusal(source, refusal)
    return PeerSnapshot(
        label=_column_label(payload, source),
        url="",
        note=_instance_field(payload, "note"),
        payload=payload,
        origin=SnapshotOrigin.FILE,
        source=source,
    )


def _snapshot_document(raw: bytes) -> tuple[dict[str, Any], str]:
    """*raw* as a snapshot payload, or an empty one and the reason it is not a snapshot."""
    if len(raw) > MAX_SNAPSHOT_BYTES:
        return {}, f"it is {len(raw)} bytes — the load limit is {MAX_SNAPSHOT_BYTES}"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {}, "it is not UTF-8 text — a snapshot is UTF-8 JSON"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"it is not valid JSON: {exc}"
    if not isinstance(payload, dict):
        return {}, f"it is a JSON {type(payload).__name__}, not a snapshot object"
    return payload, _wrong_shape(payload)


def _wrong_shape(payload: Mapping[str, Any]) -> str:
    """Why this JSON object is not a snapshot THIS teatree reads, or ``""`` when it is one."""
    if (declared := payload.get("format")) != SNAPSHOT_FORMAT:
        return f"its 'format' is {declared!r}, not {SNAPSHOT_FORMAT!r}"
    if (version := payload.get("format_version")) != FORMAT_VERSION:
        return f"its 'format_version' is {version!r} — this teatree reads format_version {FORMAT_VERSION}"
    return ""


def _column_label(payload: Mapping[str, Any], source: str) -> str:
    """The heading the loaded column sits under — its own name, then that it is a dated record."""
    name = _instance_field(payload, "label") or source
    captured = str(payload.get("captured_at", "")).strip()
    return f"{name} (file {captured})" if captured else f"{name} (file)"


def _instance_field(payload: Mapping[str, Any], key: str) -> str:
    instance = payload.get("instance")
    return str(instance.get(key, "")).strip() if isinstance(instance, Mapping) else ""


__all__ = [
    "MAX_SNAPSHOT_BYTES",
    "LoadRefusal",
    "LoadedSnapshots",
    "load_snapshots",
    "snapshot_filename",
]
