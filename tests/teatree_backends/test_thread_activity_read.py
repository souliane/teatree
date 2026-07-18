"""``read_thread_activity`` — single-thread liveness + latest-activity read (#1084 follow-up).

Fakes stop at the ``SlackHttpClient.get`` boundary — no live Slack.
"""

import httpx
import pytest

from teatree.backends.slack import client as slack_client
from teatree.backends.slack.client import SlackThreadActivityRequest, read_thread_activity
from teatree.types import RawAPIDict

_CHANNEL = "C0REVIEW"
_THREAD_TS = "1700000000.000100"


class FakeSlackHttp:
    def __init__(
        self,
        *,
        payload: dict | None = None,
        raises: BaseException | None = None,
        **_kw: object,
    ) -> None:
        self.payload = payload or {}
        self._raises = raises

    def get(self, method: str, *, token: str = "", params: dict | None = None) -> RawAPIDict:
        if self._raises is not None:
            raise self._raises
        return dict(self.payload)


def _read(fake: FakeSlackHttp) -> object:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(slack_client, "SlackHttpClient", lambda **kw: fake)
        request = SlackThreadActivityRequest(token="xoxp-user", channel_id=_CHANNEL, thread_ts=_THREAD_TS)
        return read_thread_activity(request)


FakeClient = FakeSlackHttp


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

    def test_thread_not_found_error_is_proof_of_deletion(self) -> None:
        # #3292 part 1: Slack returns ``ok:false thread_not_found`` for a deleted
        # root — that is DELETION, not a read failure. It must read as
        # ``ok=True, exists=False`` so the reclaim → re-post branch fires.
        fake = FakeClient(payload={"ok": False, "error": "thread_not_found"})
        read = _read(fake)
        assert read.ok is True
        assert read.exists is False

    def test_message_not_found_error_is_proof_of_deletion(self) -> None:
        fake = FakeClient(payload={"ok": False, "error": "message_not_found"})
        read = _read(fake)
        assert read.ok is True
        assert read.exists is False

    def test_tombstone_root_counts_as_gone(self) -> None:
        # #3292 part 2: a tombstone root (parent deleted, replies survive) must
        # NOT count as "exists" just because ``messages[0]`` is present.
        fake = FakeClient(
            payload={"ok": True, "messages": [{"ts": _THREAD_TS, "subtype": "tombstone"}, {"ts": "1700000500.000200"}]}
        )
        read = _read(fake)
        assert read.ok is True
        assert read.exists is False


class TestReadFailure:
    def test_api_not_ok_reports_not_ok(self) -> None:
        fake = FakeClient(payload={"ok": False, "error": "channel_not_found"})
        read = _read(fake)
        assert read.ok is False
        assert read.exists is False

    def test_ratelimited_is_a_read_failure_not_deletion(self) -> None:
        # #3292 part 1: every non-deletion ``ok:false`` (rate limit, auth) stays
        # fail-safe (read failure ⇒ suppress) — never mistaken for "gone".
        fake = FakeClient(payload={"ok": False, "error": "ratelimited"})
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
