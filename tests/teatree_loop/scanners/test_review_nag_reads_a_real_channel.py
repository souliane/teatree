"""The nag runs against the REAL guard, so it cannot reconcile against its own root.

Every other nag test patches ``reconcile_out_of_band`` out through an autouse fixture,
which is precisely why this defect shipped: under that fixture the guard never runs,
so a nag that stops itself on the first due tick reads exactly like one that posts.
This module deliberately declares no such fixture and drives the real guard over a
stubbed channel history.
"""

import datetime as dt
from typing import TYPE_CHECKING, cast

import httpx
import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.backends.slack import http as slack_http
from teatree.core.gates.review_request_guard import GuardTarget
from teatree.core.models import ReviewRequestPost
from teatree.loop.scanners.review_nag import ReviewNagScanner
from tests.teatree_core.test_review_request_guard import _HUMAN_AUTHOR, FakeClient
from tests.teatree_loop.test_review_nag_scanner import FakeHost, FakeSlack, _PermittingPostureMixin

if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend

_CHANNEL = "C0DEMOCHAN1"
_MR_URL = "https://gitlab.com/org/repo/-/merge_requests/385"


def _ts_now() -> str:
    return f"{timezone.now().timestamp():.6f}"


def _ts_days_ago(days: float) -> str:
    return f"{(timezone.now() - dt.timedelta(days=days)).timestamp():.6f}"


def _scanner(slack: FakeSlack) -> ReviewNagScanner:
    messaging = cast("MessagingBackend", slack)
    return ReviewNagScanner(messaging=messaging, host=FakeHost(), identities=("owner",))


def _point_the_guard_at(mp: pytest.MonkeyPatch, fake: FakeClient) -> None:
    target = GuardTarget(channel_id=_CHANNEL, channel_name="review", token="xoxb-test")
    mp.setattr(slack_http.httpx, "get", fake.get)
    mp.setattr("teatree.core.gates.review_request_guard.resolve_guard_target", lambda **_kwargs: target)


class TestTheNagDoesNotReconcileAgainstItsOwnRoot(_PermittingPostureMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root_ts = _ts_days_ago(3)
        self.post = ReviewRequestPost.objects.create(
            mr_url=_MR_URL,
            overlay="",
            slack_channel_id=_CHANNEL,
            slack_thread_ts=self.root_ts,
            created_at=timezone.now() - dt.timedelta(days=3),
        )

    def _root(self) -> dict[str, object]:
        return {"text": f"review {_MR_URL}", "ts": self.root_ts, "user": _HUMAN_AUTHOR}

    def _scan(self, messages: list[dict[str, object]], slack: FakeSlack) -> list[str]:
        fake = FakeClient(pages=[{"ok": True, "messages": messages, "has_more": False}])
        with pytest.MonkeyPatch.context() as mp:
            _point_the_guard_at(mp, fake)
            return [signal.kind for signal in _scanner(slack).scan()]

    def test_a_channel_holding_only_the_tracked_root_still_gets_the_re_ping(self) -> None:
        slack = FakeSlack()

        assert self._scan([self._root()], slack) == ["review_nag.ping"]
        assert len(slack.posts) == 1
        self.post.refresh_from_db()
        # done_at is also what the merge-react and the resume read, so setting it here
        # would retire all three on the first due tick.
        assert self.post.done_at is None

    def test_a_genuine_second_request_still_stops_the_train(self) -> None:
        slack = FakeSlack()
        # `conversations.history` pages newest-first, so a later request precedes the root.
        second = {"text": f"anyone free for {_MR_URL}?", "ts": _ts_now(), "user": _HUMAN_AUTHOR}

        assert self._scan([second, self._root()], slack) == ["review_nag.reconciled"]
        assert slack.posts == []
        self.post.refresh_from_db()
        assert self.post.done_at is not None

    def test_an_unreadable_channel_holds_the_nag(self) -> None:
        slack = FakeSlack()
        with pytest.MonkeyPatch.context() as mp:
            _point_the_guard_at(mp, FakeClient(raises=httpx.HTTPError("down")))
            kinds = [signal.kind for signal in _scanner(slack).scan()]

        assert kinds == ["review_nag.guard_unreadable"]
        assert slack.posts == []
