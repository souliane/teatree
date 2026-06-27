"""Loop control for the review chokepoint is DB-only — ``T3_LOOPS_DISABLED`` is inert.

The ``T3_LOOPS_DISABLED`` env kill-switch is removed: ``review_loop_enabled``
(the discovery-time review-claim gate) resolves through the DB ``LoopState`` tier
only. Two halves of the cutover are pinned anti-vacuously. First, a set
``T3_LOOPS_DISABLED`` env var (``review`` or ``all``) has NO effect — the review
loop stays enabled (RED on the pre-cutover env-tier code, which dropped every
review-intent signal under a leaked env; GREEN now). Second, a durable DB
``LoopState`` DISABLE/PAUSE on ``review`` IS what suppresses — the same control
outcome the env tier used to provide, now DB-only.

The broadcast scanner is exercised end to end under a poisoned env to prove the
discovery path no longer reads it.
"""

import pytest
from django.test import TestCase

from teatree.core.models import LoopState
from teatree.loop.review_claim_signals import review_loop_enabled
from teatree.loop.scanners.slack_broadcasts import MrState, SlackBroadcastsScanner
from teatree.types import RawAPIDict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_CHANNEL = "C0REVIEWDB"
_TS = "1779990002.000002"
_MR = "https://gitlab.example.com/team/project/-/merge_requests/2584"


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


class TestEnvKillSwitchIsInert(TestCase):
    def test_env_all_does_not_disable_review(self) -> None:
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("T3_LOOPS_DISABLED", "all")
            assert review_loop_enabled() is True

    def test_env_named_review_does_not_disable_review(self) -> None:
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("T3_LOOPS_DISABLED", "review")
            assert review_loop_enabled() is True

    def test_broadcast_scanner_still_emits_review_intent_under_poisoned_env(self) -> None:
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("T3_LOOPS_DISABLED", "all")
            scanner = SlackBroadcastsScanner(
                backend=_Backend(),
                channels=[_CHANNEL],
                fetch_channel_history=_fetcher([{"text": f"please review {_MR}", "ts": _TS, "type": "message"}]),
                classify_mrs=_classifier(MrState(url=_MR, merged=False, approved=False, author_username="colleague")),
            )
            signals = scanner.scan()
        assert [s.kind for s in signals] == ["slack.review_intent"]


class TestDbLoopStateIsTheControl(TestCase):
    def test_db_disable_suppresses_review_even_with_env_unset(self) -> None:
        LoopState.objects.disable("review")
        assert review_loop_enabled() is False

    def test_db_disable_suppresses_review_even_with_env_set_inert(self) -> None:
        # The DB is the only control: a DISABLED row suppresses regardless of any
        # (now-inert) env value.
        LoopState.objects.disable("review")
        with pytest.MonkeyPatch().context() as mp:
            mp.setenv("T3_LOOPS_DISABLED", "")  # env says "not disabled" — still suppressed by DB
            assert review_loop_enabled() is False

    def test_db_pause_suppresses_review(self) -> None:
        LoopState.objects.pause("review")
        assert review_loop_enabled() is False

    def test_no_db_row_leaves_review_enabled(self) -> None:
        assert review_loop_enabled() is True
