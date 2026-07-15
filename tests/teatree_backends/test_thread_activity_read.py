"""``read_thread_activity`` — single-thread liveness + latest-activity read (#1084 follow-up).

Fakes stop at the ``conversations.replies`` httpx boundary — no live Slack.
"""

from typing import Self

import httpx
import pytest

from teatree.backends.slack import client as slack_client
from teatree.backends.slack.client import SlackThreadActivityRequest, read_thread_activity

_CHANNEL = "C0REVIEW"
_THREAD_TS = "1700000000.000100"


class FakeClient:
    def __init__(
        self,
        *,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
        raises: BaseException | None = None,
        **_kw: object,
    ) -> None:
        self.payload = payload or {}
        self.headers = headers or {}
        self._raises = raises

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def get(self, url: str, **_kw: object) -> httpx.Response:
        if self._raises is not None:
            raise self._raises
        return httpx.Response(200, json=self.payload, request=httpx.Request("GET", url))


def _bind(fake: FakeClient, kw: dict) -> FakeClient:
    fake.headers = kw.get("headers", fake.headers)
    return fake


def _read(fake: FakeClient) -> object:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(slack_client.httpx, "Client", lambda **kw: _bind(fake, kw))
        request = SlackThreadActivityRequest(token="xoxp-user", channel_id=_CHANNEL, thread_ts=_THREAD_TS)
        return read_thread_activity(request)


class TestThreadStillLive:
    def test_parent_present_reports_exists_and_parent_ts(self) -> None:
        fake = FakeClient(payload={"ok": True, "messages": [{"ts": _THREAD_TS, "text": "please review"}]})
        read = _read(fake)
        assert read.ok is True
        assert read.exists is True
        assert read.parent_ts == _THREAD_TS
        assert read.latest_reply_ts == ""
        assert read.has_reaction is False

    def test_latest_reply_ts_is_the_newest_reply(self) -> None:
        fake = FakeClient(
            payload={
                "ok": True,
                "messages": [
                    {"ts": _THREAD_TS},
                    {"ts": "1700000500.000200"},
                    {"ts": "1700000900.000300"},
                ],
            }
        )
        read = _read(fake)
        assert read.exists is True
        assert read.latest_reply_ts == "1700000900.000300"

    def test_reaction_on_parent_reports_has_reaction(self) -> None:
        fake = FakeClient(
            payload={"ok": True, "messages": [{"ts": _THREAD_TS, "reactions": [{"name": "eyes", "count": 1}]}]}
        )
        read = _read(fake)
        assert read.has_reaction is True


class TestThreadGone:
    def test_empty_messages_reports_not_exists_but_ok(self) -> None:
        fake = FakeClient(payload={"ok": True, "messages": []})
        read = _read(fake)
        assert read.ok is True
        assert read.exists is False


class TestReadFailure:
    def test_api_not_ok_reports_not_ok(self) -> None:
        fake = FakeClient(payload={"ok": False, "error": "channel_not_found"})
        read = _read(fake)
        assert read.ok is False
        assert read.exists is False

    def test_transport_error_propagates(self) -> None:
        fake = FakeClient(raises=httpx.TimeoutException("slow"))
        with pytest.raises(httpx.HTTPError):
            _read(fake)


class TestEmptyRequest:
    def test_blank_thread_ts_short_circuits_to_absent(self) -> None:
        read = read_thread_activity(SlackThreadActivityRequest(token="t", channel_id=_CHANNEL, thread_ts=""))
        assert read.ok is True
        assert read.exists is False
