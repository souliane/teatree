"""Proving nobody has been asked — the one fact the triage ladder may not guess at.

The ``ReviewRequestPost`` ledger answers REQUESTED and nothing else. A request
made before the ledger existed, or in a repo whose channel nothing watches,
leaves no row either, so an absent row is silence rather than evidence. NONE has
to be EARNED from the review channel itself, and only over the stretch the read
actually covered: a clean thirty-day read says nothing whatever about a merge
request opened ninety days ago.

Every case below is one way that earning can fail. They matter because NONE is
the state that unlocks the ladder's review-request rung — the only fact standing
between a silent backlog and a broadcast nobody authorised.
"""

import datetime as dt
from unittest.mock import patch

from django.test import SimpleTestCase

from teatree.core.backend_registry import ReviewSearchSpec
from teatree.core.gates.review_request_guard import GuardOptions, GuardTarget, LiveAskRead
from teatree.core.review.mr_ask_state import mr_ask_state, read_asks
from teatree.core.review.mr_triage import ReviewRequestState

_MR = "https://git.example.com/acme/app/-/merge_requests/41"
_SIBLING = "https://git.example.com/acme/app/-/merge_requests/42"
_NOW = dt.datetime(2026, 8, 19, 12, 0, tzinfo=dt.UTC)
_TARGET = GuardTarget(channel_id="C1", channel_name="reviews", token="xoxb-bot")
_OPTIONS = GuardOptions(recency_window=dt.timedelta(days=30), now=_NOW)


class _Provider:
    """The channel read, staged: ``ok`` false is a FAILED read, never an empty one."""

    def __init__(self, *, ok: bool = True, urls: tuple[str, ...] = ()) -> None:
        self._ok = ok
        self._urls = urls
        self.asked_for: list[str] = []

    def read_recent_review_matches(self, spec: ReviewSearchSpec) -> object:
        self.asked_for.append(",".join(sorted(spec.pr_urls)))
        matches = [type("M", (), {"pr_url": url, "ts": f"{_NOW.timestamp():.6f}"})() for url in self._urls]
        return type("R", (), {"ok": self._ok, "matches": matches})()


def _read(
    provider: _Provider,
    *,
    urls: tuple[str, ...] = (_MR,),
    target: GuardTarget | None = _TARGET,
) -> LiveAskRead | None:
    with patch("teatree.core.backend_registry.get_backend_provider", return_value=provider):
        return read_asks(urls, target=target, options=_OPTIONS)


def _state(provider: _Provider, *, opened_at: dt.datetime | None) -> ReviewRequestState:
    return mr_ask_state(_MR, opened_at=opened_at, read=_read(provider))


class TestNoneIsEarnedFromTheChannel(SimpleTestCase):
    def test_a_clean_read_covering_the_whole_open_life_proves_nobody_was_asked(self) -> None:
        state = _state(_Provider(), opened_at=_NOW - dt.timedelta(days=3))

        assert state is ReviewRequestState.NONE

    def test_a_match_in_the_window_is_an_ask_the_ledger_never_recorded(self) -> None:
        state = _state(_Provider(urls=(_MR,)), opened_at=_NOW - dt.timedelta(days=3))

        assert state is ReviewRequestState.REQUESTED


class TestEveryUnprovableAnswerStaysUnknown(SimpleTestCase):
    def test_a_merge_request_older_than_the_window_is_not_proved_unasked(self) -> None:
        """The read covered thirty days; the request has been open for ninety."""
        state = _state(_Provider(), opened_at=_NOW - dt.timedelta(days=90))

        assert state is ReviewRequestState.UNKNOWN

    def test_a_failed_read_never_becomes_a_clean_one(self) -> None:
        state = _state(_Provider(ok=False), opened_at=_NOW - dt.timedelta(days=3))

        assert state is ReviewRequestState.UNKNOWN

    def test_an_unreadable_open_date_leaves_the_window_unmeasurable(self) -> None:
        state = _state(_Provider(), opened_at=None)

        assert state is ReviewRequestState.UNKNOWN

    def test_no_postable_review_channel_is_silence_not_absence(self) -> None:
        read = _read(_Provider(), target=None)

        assert mr_ask_state(_MR, opened_at=_NOW - dt.timedelta(days=3), read=read) is ReviewRequestState.UNKNOWN


class TestTheWholeListingCostsOneChannelWalk(SimpleTestCase):
    """The history walk is paginated and rate-limited, so it is asked once per tick, not once per MR."""

    def test_two_merge_requests_are_answered_from_a_single_read(self) -> None:
        provider = _Provider(urls=(_SIBLING,))

        read = _read(provider, urls=(_MR, _SIBLING))

        assert provider.asked_for == [f"{_MR},{_SIBLING}"]
        assert mr_ask_state(_SIBLING, opened_at=_NOW, read=read) is ReviewRequestState.REQUESTED
        assert mr_ask_state(_MR, opened_at=_NOW, read=read) is ReviewRequestState.NONE
