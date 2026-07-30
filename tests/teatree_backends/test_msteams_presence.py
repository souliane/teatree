"""MS Teams presence backend — MS Graph ``/me/presence`` → ``Presence`` (#2171).

Pins the classification (``Busy`` / ``InAConferenceCall`` / ``Presenting`` →
``IN_MEETING``; other known states → ``FREE``; missing/unclassifiable →
``UNKNOWN``) and the fail-safe contract: no token, a network error, or a
non-dict body all resolve to ``UNKNOWN`` (never a spurious ``IN_MEETING`` that
would mute audio). The HTTP call is injected so no test hits Graph.
"""

from unittest.mock import MagicMock, patch

from teatree.backends.msteams import presence as ms
from teatree.backends.msteams.presence import MsTeamsPresenceBackend
from teatree.core.presence import Presence


def _backend(body: object, *, access: str = "graph-access") -> MsTeamsPresenceBackend:
    return MsTeamsPresenceBackend(access_token=access, http_get=lambda *, access_token: body)


class TestClassification:
    def test_busy_availability_is_in_meeting(self) -> None:
        assert _backend({"availability": "Busy", "activity": "InACall"}).current_presence() is Presence.IN_MEETING

    def test_in_a_conference_call_activity_is_in_meeting(self) -> None:
        b = _backend({"availability": "Available", "activity": "InAConferenceCall"})
        assert b.current_presence() is Presence.IN_MEETING

    def test_presenting_activity_is_in_meeting(self) -> None:
        assert (
            _backend({"availability": "Available", "activity": "Presenting"}).current_presence() is Presence.IN_MEETING
        )

    def test_available_is_free(self) -> None:
        assert _backend({"availability": "Available", "activity": "Available"}).current_presence() is Presence.FREE

    def test_away_is_free_not_meeting(self) -> None:
        assert _backend({"availability": "Away", "activity": "Away"}).current_presence() is Presence.FREE

    def test_presence_unknown_availability_is_unknown(self) -> None:
        b = _backend({"availability": "PresenceUnknown", "activity": "PresenceUnknown"})
        assert b.current_presence() is Presence.UNKNOWN

    def test_classification_is_case_insensitive(self) -> None:
        assert _backend({"availability": "busy"}).current_presence() is Presence.IN_MEETING


class TestFailSafe:
    def test_empty_token_is_unknown(self) -> None:
        called: list[int] = []
        b = MsTeamsPresenceBackend(access_token="", http_get=lambda *, access_token: called.append(1) or {})
        assert b.current_presence() is Presence.UNKNOWN
        assert called == []  # never even calls Graph without a token

    def test_none_body_is_unknown(self) -> None:
        assert _backend(None).current_presence() is Presence.UNKNOWN

    def test_non_dict_body_is_unknown(self) -> None:
        assert _backend(["not", "a", "dict"]).current_presence() is Presence.UNKNOWN

    def test_http_error_is_unknown(self) -> None:
        def boom(*, access_token: str) -> dict:
            msg = "graph 503"
            raise RuntimeError(msg)

        b = MsTeamsPresenceBackend(access_token="tok", http_get=boom)
        assert b.current_presence() is Presence.UNKNOWN


class TestDefaultGraphGet:
    """The real ``_graph_get`` calls MS Graph with the bearer token (httpx mocked)."""

    def test_calls_graph_with_bearer_and_returns_json(self) -> None:
        response = MagicMock()
        response.json.return_value = {"availability": "Busy"}
        with patch.object(ms.httpx, "get", return_value=response) as get:
            body = ms._graph_get(access_token="graph-access")
        assert body == {"availability": "Busy"}
        response.raise_for_status.assert_called_once()
        url, kwargs = get.call_args.args, get.call_args.kwargs
        assert url[0] == ms._GRAPH_PRESENCE_URL
        assert kwargs["headers"]["Authorization"] == "Bearer graph-access"

    def test_default_backend_uses_graph_get_end_to_end(self) -> None:
        response = MagicMock()
        response.json.return_value = {"availability": "Presenting"}
        with patch.object(ms.httpx, "get", return_value=response):
            assert MsTeamsPresenceBackend(access_token="t").current_presence() is Presence.IN_MEETING
