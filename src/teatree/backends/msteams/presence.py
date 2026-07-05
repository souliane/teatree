"""MS Teams meeting-presence backend — MS Graph ``/me/presence`` (#2171).

The concrete :class:`~teatree.core.presence.PresenceBackend` behind
``[teatree.speak] presence_backend = "msteams"``. It reads the signed-in user's
Teams presence over MS Graph and maps it to a :class:`Presence`, so local TTS
mutes while the user is on a call.

**Auth.** The backend authenticates with a Graph OAuth *access token* carrying
the ``Presence.Read`` delegated scope, stored in the ``pass`` password store
under the entry named by ``[teatree.speak] presence_token_ref`` and read via
:func:`teatree.utils.secrets.read_pass` at build time. Acquiring that token
(app registration → delegated ``Presence.Read`` consent → token refresh) is an
operator step documented in ``docs/blueprint/configuration.md``; teatree only
consumes an already-issued token.

Fail-safe by construction: an empty token, a Graph error, or an
unclassifiable body all resolve to :attr:`Presence.UNKNOWN` (do NOT mute) — a
presence probe must never SILENCE audio on its own uncertainty, only on a
positive in-meeting signal.
"""

from collections.abc import Callable
from typing import TypedDict, cast

import httpx

from teatree.core.presence import Presence


class GraphPresenceBody(TypedDict, total=False):
    """The subset of the MS Graph ``/me/presence`` response teatree reads."""

    availability: str
    activity: str


_GRAPH_PRESENCE_URL = "https://graph.microsoft.com/v1.0/me/presence"
_GRAPH_TIMEOUT_SECONDS = 4.0

# The Graph ``availability`` / ``activity`` tokens that mean "in a meeting".
# ``Busy`` is an availability; ``InAConferenceCall`` / ``Presenting`` are
# activities — both fields are checked against all three (case-folded).
_MEETING_TOKENS = frozenset({"busy", "inaconferencecall", "presenting"})

# Availability values that carry no usable signal — treated as UNKNOWN so an
# offline / not-yet-resolved account never forces a FREE verdict.
_UNKNOWN_AVAILABILITY = frozenset({"", "presenceunknown"})

type GraphGet = Callable[..., object]


def _graph_get(*, access_token: str) -> object:
    """GET ``/me/presence`` from MS Graph; return the parsed JSON body (raises on failure)."""
    response = httpx.get(
        _GRAPH_PRESENCE_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=_GRAPH_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


class MsTeamsPresenceBackend:
    """Resolve Teams meeting presence over MS Graph.

    ``http_get`` is injected (defaults to the real Graph GET) so tests and
    offline callers supply the body directly without touching the network.
    """

    def __init__(self, *, access_token: str, http_get: GraphGet | None = None) -> None:
        self._access_token = access_token
        self._http_get = http_get or _graph_get

    def current_presence(self) -> Presence:
        if not self._access_token:
            return Presence.UNKNOWN
        try:
            body = self._http_get(access_token=self._access_token)
        except Exception:  # noqa: BLE001 — any Graph/transport failure is a non-signal, never a mute
            return Presence.UNKNOWN
        return _classify(body)


def _norm(value: object) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _classify(body: object) -> Presence:
    if not isinstance(body, dict):
        return Presence.UNKNOWN
    data = cast("GraphPresenceBody", body)
    availability = _norm(data.get("availability"))
    activity = _norm(data.get("activity"))
    if availability in _MEETING_TOKENS or activity in _MEETING_TOKENS:
        return Presence.IN_MEETING
    if availability in _UNKNOWN_AVAILABILITY:
        return Presence.UNKNOWN
    return Presence.FREE
