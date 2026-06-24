"""Conftest hermeticity: a leaked ``T3_LOOPS_DISABLED`` cannot disable review (#2359 Class B).

The discovery-time review-intent scanner tests (``test_review_claim_discipline``,
``test_per_item_fault_isolation_1597``) pass in isolation but failed under a
full-suite collection order because ``T3_LOOPS_DISABLED`` leaked a value
containing ``review`` (or ``all``) into the process env. ``review_loop_enabled``
reads that env var at call time, so a leak makes ``filter_review_intent_signals``
drop every broadcast review-intent signal.

The ``_isolate_env`` autouse conftest fixture now clears ``T3_LOOPS_DISABLED`` per
test. This module proves the isolation end to end: the OS env is poisoned at
import time (before any fixture runs), and the fixture must have cleared it by the
time a test body executes — so the broadcast scanner still emits the review-intent
signal regardless of host/cross-test env pollution.
"""

import os

import pytest
from django.test import TestCase

from teatree.loop.review_claim_signals import review_loop_enabled
from teatree.loop.scanners.slack_broadcasts import MrState, SlackBroadcastsScanner
from teatree.types import RawAPIDict

# Poison the OS env at COLLECTION time — before any per-test fixture runs — so the
# autouse ``_isolate_env`` conftest fixture has a leaked value to clear. Without
# the fixture's ``delenv("T3_LOOPS_DISABLED")``, this leak survives into the test
# body and the scanner drops the review-intent signal.
os.environ["T3_LOOPS_DISABLED"] = "review"

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_CHANNEL = "C0REVIEW2359"
_TS = "1779990002.000002"
_MR = "https://gitlab.example.com/team/project/-/merge_requests/2359"


def _fetcher(messages: list[RawAPIDict]):
    def fetch(*, channel: str) -> list[RawAPIDict]:
        _ = channel
        return list(messages)

    return fetch


def _classifier(state: MrState):
    def classify(urls: list[str]) -> list[MrState]:
        return [state for _ in urls]

    return classify


class _Backend:
    user_id = "U0DEMOUSER1"

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        _ = (channel, ts, emoji)
        return {"ok": True}


class TestLeakedLoopsDisabledIsCleared(TestCase):
    def test_env_var_is_cleared_by_isolate_env_fixture(self) -> None:
        # The module poisoned the OS env at import; the conftest fixture clears it.
        assert os.environ.get("T3_LOOPS_DISABLED") is None
        assert review_loop_enabled() is True

    def test_broadcast_scanner_emits_review_intent_despite_leaked_env(self) -> None:
        scanner = SlackBroadcastsScanner(
            backend=_Backend(),
            channels=[_CHANNEL],
            fetch_channel_history=_fetcher([{"text": f"please review {_MR}", "ts": _TS, "type": "message"}]),
            classify_mrs=_classifier(MrState(url=_MR, merged=False, approved=False, author_username="colleague")),
        )
        signals = scanner.scan()
        assert [s.kind for s in signals] == ["slack.review_intent"]
