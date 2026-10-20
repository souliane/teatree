"""Whether the factory's Slack integration is live: the one predicate the relay and its hooks agree on.

Live means all four legs hold: an owner-DM transport resolves, it carries a configured owner id, its
Socket Mode heartbeat stamp is fresh, and the ``t3-master`` lease that gates the ``slack_answer`` cycle
is held. Each absent leg is named in :attr:`SlackLiveVerdict.reason`, and any failure to read a leg is
not live. This is deliberately stricter than the doctor's listener probe, which passes a box with no
heartbeat file at all.

The verdict is judged on the overlay that carries the owner-DM transport, not on the caller's: a
session in a noop overlay on a box whose Slack lives in a sibling overlay is still Slack-first.
"""

import time
from dataclasses import dataclass

from teatree.core.backend_factory import OwnerMessagingTransport
from teatree.core.backend_protocols import MessagingBackend
from teatree.core.gates.t3_master_gate import t3_master_lease_held
from teatree.core.owner_identity import configured_owner_id
from teatree.core.slack_listener_heartbeat import heartbeat_is_stale, read_heartbeat


@dataclass(frozen=True, slots=True)
class SlackLiveVerdict:
    overlay: str
    live: bool
    reason: str = ""
    backend: MessagingBackend | None = None
    owner_id: str = ""


def owner_transport(preferred: str = "") -> tuple[str, MessagingBackend] | str:
    """The ``(overlay, backend)`` that carries the owner DM, or the reason none can be named.

    The preferred overlay wins when it is credentialed, else the one credentialed overlay; none or several
    is a named reason rather than a guess.
    """
    credentialed = OwnerMessagingTransport.credentialed()
    for name, backend in credentialed:
        if name == preferred:
            return name, backend
    if len(credentialed) == 1:
        return credentialed[0]
    return (
        "ambiguous_overlay: several overlays carry a messaging transport"
        if credentialed
        else ("no_messaging_backend: no overlay carries a real messaging transport")
    )


def factory_slack_live(overlay: str = "", *, now: float | None = None) -> SlackLiveVerdict:
    """The factory-Slack verdict for the owner transport reached from *overlay* (``now``: epoch seconds)."""
    try:
        return _verdict(overlay, time.time() if now is None else now)
    except Exception as exc:  # noqa: BLE001 — an unreadable leg is not live, never a crash in a caller's turn
        return SlackLiveVerdict(overlay=overlay, live=False, reason=f"error: {type(exc).__name__}: {exc}")


def _verdict(overlay: str, now: float) -> SlackLiveVerdict:
    transport = owner_transport(overlay)
    if isinstance(transport, str):
        return SlackLiveVerdict(overlay=overlay, live=False, reason=transport)
    name, backend = transport
    owner_id = configured_owner_id(backend)
    if not owner_id:
        return SlackLiveVerdict(overlay=name, live=False, reason="no_owner_id: the transport has no configured owner")
    beat = read_heartbeat()
    if beat is None:
        return SlackLiveVerdict(overlay=name, live=False, reason="heartbeat_absent: no readable listener heartbeat")
    if heartbeat_is_stale(beat, name, now=now):
        return SlackLiveVerdict(overlay=name, live=False, reason=f"heartbeat_stale: no fresh stamp for {name}")
    if not t3_master_lease_held():
        return SlackLiveVerdict(overlay=name, live=False, reason="t3_master_unheld: the t3-master lease is not held")
    return SlackLiveVerdict(overlay=name, live=True, backend=backend, owner_id=owner_id)
