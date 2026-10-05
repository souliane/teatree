"""Deliver the standing directives into a turn that is already happening (#4166) — never a registration.

The Claude-plugin adapter of the harness-neutral model in :mod:`teatree.loop.standing_directives`: the
texts, the cadences, the scopes and the mode brake belong there, and this module only maps the published
list onto this harness's context channel. Every slot reaches an engaged session when it starts, and comes back on
the first tool call after an owner prompt once its own cadence has passed. Nothing is registered and
nothing wakes the session, so a delivery costs no turn. An ``attended-singleton`` slot reaches only the
session that owns the host's loop slot in ``loop-registry.json``, read and never claimed, so a host-wide
slot is driven once per host.

Django-free: it reads the file the worker publishes (:mod:`teatree.standing_directives_cache`) and,
while nothing readable is published, the compiled defaults. An engaged session is the owner's ``autoload``, or
a teatree or ``t3:`` skill loaded in this session (the ``.teatree-active`` / ``.t3-engaged`` markers);
a factory worker on the SDK lane is FSM-governed and gets nothing. ``<session>.directives-injected``
holds each slot's last delivery instant, written only once the caller has delivered them (a failed or
overrun write leaves them due), replaced atomically, and left to the router's age sweep.
"""

import json
import os
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from hooks.scripts.loop_registry_liveness import session_owns_loop
from hooks.scripts.session_lane import LANE_SDK, session_lane
from hooks.scripts.state_files import hook_state_dir
from hooks.scripts.teatree_settings import autoload_enabled
from teatree import standing_directives_cache
from teatree.loop.standing_directives import SCOPE_ATTENDED_SINGLETON, compiled_directives
from teatree.standing_directives_cache import StandingDirectivePayload

INJECTED_MARKER = "directives-injected"
_ENGAGEMENT_MARKERS = ("teatree-active", "t3-engaged")


@dataclass(frozen=True, slots=True)
class DueRules:
    """The standing directives due for one session, and the delivery instants to keep once they reached it."""

    session_id: str
    directives: tuple[StandingDirectivePayload, ...] = ()
    stamps: dict[str, float] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """The rendered directives, or ``""`` when none is due."""
        return _render(self.directives) if self.directives else ""

    def mark_delivered(self) -> None:
        """Keep each handed-out slot's delivery instant — call it only once the text was written."""
        if self.directives:
            _keep(self.session_id, self.stamps)


def due_rules(session_id: str, *, every_slot: bool) -> DueRules:
    """The standing directives to put in front of *session_id* now.

    *every_slot* hands out each slot however recently it went out (a session start); otherwise only the
    slots whose cadence has passed since this session last received them. Nothing is recorded until
    :meth:`DueRules.mark_delivered`.
    """
    if not session_id or session_lane() == LANE_SDK or not _engaged(session_id):
        return DueRules(session_id)
    stamps = _stamps(session_id)
    now = time.time()
    due = tuple(
        directive
        for directive in _reaching(session_id)
        if every_slot or now - stamps.get(directive["slot_id"], 0.0) >= directive["cadence_seconds"]
    )
    return DueRules(session_id, due, stamps | dict.fromkeys((directive["slot_id"] for directive in due), now))


def _engaged(session_id: str) -> bool:
    state = hook_state_dir()
    return any((state / f"{session_id}.{marker}").is_file() for marker in _ENGAGEMENT_MARKERS) or autoload_enabled()


def _reaching(session_id: str) -> list[StandingDirectivePayload]:
    """The published directives this session may receive; a host-wide slot only by the loop-slot owner."""
    published = standing_directives_cache.read()
    directives = [directive.as_dict() for directive in compiled_directives()] if published is None else published
    if any(directive["scope"] == SCOPE_ATTENDED_SINGLETON for directive in directives) and not session_owns_loop(
        session_id
    ):
        return [directive for directive in directives if directive["scope"] != SCOPE_ATTENDED_SINGLETON]
    return directives


def _marker(session_id: str) -> Path:
    return hook_state_dir() / f"{session_id}.{INJECTED_MARKER}"


def _stamps(session_id: str) -> dict[str, float]:
    try:
        parsed = json.loads(_marker(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {slot: float(stamp) for slot, stamp in parsed.items() if isinstance(stamp, int | float)}


def _keep(session_id: str, stamps: dict[str, float]) -> None:
    """Replace the marker with *stamps*; with an unwritable state dir the next look simply delivers again."""
    marker = _marker(session_id)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        handle, staged = tempfile.mkstemp(prefix=f".{marker.name}.", dir=marker.parent)
    except OSError:
        return
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(stamps, stream)
        Path(staged).replace(marker)
    except OSError:
        return
    finally:
        Path(staged).unlink(missing_ok=True)


def _render(directives: Sequence[StandingDirectivePayload]) -> str:
    lines = [f"Standing rules for this session ({len(directives)}) — they apply to your next reply:"]
    lines.extend(f"  - [{directive['slot_id']}] {directive['text']}" for directive in directives)
    return "\n".join(lines)


__all__ = ["INJECTED_MARKER", "DueRules", "due_rules"]
