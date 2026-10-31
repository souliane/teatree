"""The backlog nag claims only the bumps it posts, each with the real age, and posts nothing at night (#5096)."""

import datetime as dt
import zoneinfo
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.models import DeferredQuestion
from teatree.core.owner_question_message import age_phrase
from teatree.loop.scanners.question_backlog_nag import QuestionBacklogNagScanner
from tests._owner_channel import OWNER_CARD

pytestmark = pytest.mark.real_quiet_hours

_PARIS = zoneinfo.ZoneInfo("Europe/Paris")
_AGES = (dt.timedelta(hours=3, minutes=5), dt.timedelta(hours=5, minutes=5), dt.timedelta(hours=12, minutes=5))
_HELD = dt.timedelta(minutes=20)


def _backend() -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = "D0DEMOOWNER"
    backend.post_message.return_value = {"ok": True, "ts": "1800000000.000100"}
    backend.get_permalink.return_value = "https://acme.slack.example/archives/D0DEMOOWNER/p1800000000000100"
    return backend


def _mirrored_cards(now: dt.datetime) -> None:
    for index, age in enumerate((*_AGES, _HELD)):
        posted = now - age
        DeferredQuestion.record(
            f"Should the factory pick option {index}?",
            card=OWNER_CARD,
            slack_channel="D0DEMOOWNER",
            slack_ts=f"{posted.timestamp():.6f}",
        )


def _scan(now: dt.datetime, backend: MagicMock) -> list:
    with patch("django.utils.timezone.now", return_value=now.astimezone(dt.UTC)):
        return QuestionBacklogNagScanner(backend=backend, user_id="U0DEMOOWNER").scan()


class TestTheNagIsTruthful(TestCase):
    def test_the_reported_bumps_equal_the_posts_and_each_text_states_the_real_age(self) -> None:
        now = dt.datetime(2026, 10, 12, 12, 0, tzinfo=_PARIS)
        _mirrored_cards(now)
        backend = _backend()

        (signal,) = _scan(now, backend)

        texts = [call.kwargs["text"] for call in backend.post_message.call_args_list]
        assert signal.payload["bumped"] == len(texts) == len(_AGES)
        assert signal.payload["mirrored_pending"] == len(_AGES) + 1
        for age in _AGES:
            assert any(age_phrase(age) in text for text in texts)
        assert not any("most urgent" in text or "oldest" in text for text in texts)

    def test_at_night_nothing_posts_and_no_signal_is_raised(self) -> None:
        now = dt.datetime(2026, 10, 12, 23, 30, tzinfo=_PARIS)
        _mirrored_cards(now)
        backend = _backend()

        assert _scan(now, backend) == []

        backend.post_message.assert_not_called()
