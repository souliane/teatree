"""Branch coverage for the cycle internals (#1014).

Covers the CAS-lost paths, the readback-exception path, the Stage-B-bail
→ delegation fall-through, the no-backend skip, and the production
``_default_resolver`` (factory) seam.
"""

from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from teatree.core.models import PendingChatInjection, Task
from teatree.loop.slack_answer.cycle import (
    SlackAnswerReport,
    _default_resolver,
    _delegate_needs_work,
    _handle_ack,
    _process_unit,
    _react_eyes_once,
    _Unit,
    run_slack_answer_cycle,
    verify_reply_visible,
)
from teatree.types import RawAPIDict

pytestmark = pytest.mark.django_db


@dataclass
class RecordingBackend:
    reactions: list[tuple[str, str, str]] = field(default_factory=list)
    replies: list[tuple[str, str, str]] = field(default_factory=list)

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        self.replies.append((channel, ts, text))
        return {"ok": True}

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return "D1"

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = (channel, ts)
        return "https://slack/p1"

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.reactions.append((channel, ts, emoji))
        return {"ok": True}

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""


@dataclass
class FailingReactBackend(RecordingBackend):
    """A backend whose ``react`` always raises — models a Slack outage."""

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        _ = (channel, ts, emoji)
        msg = "slack react failed"
        raise RuntimeError(msg)


def _row(text: str, ts: str = "1.0") -> PendingChatInjection:
    row = PendingChatInjection.record(channel="C1", slack_ts=ts, text=text)
    assert row is not None
    return row


class TestVerifyReplyVisible:
    def test_exception_is_conservative_false(self) -> None:
        class Boom:
            def get_permalink(self, *, channel: str, ts: str) -> str:
                msg = "network down"
                raise RuntimeError(msg)

        assert verify_reply_visible(Boom(), channel="C", ts="1.0") is False


class TestReactEyesOnce:
    def test_already_reacted_short_circuits(self) -> None:
        row = _row("thanks")
        row.mark_eyes_reacted()
        backend = RecordingBackend()

        assert _react_eyes_once(backend, _Unit([row])) is False
        assert backend.reactions == []

    def test_cas_lost_returns_false(self) -> None:
        row = _row("thanks")
        backend = RecordingBackend()
        with patch.object(type(row), "mark_eyes_reacted", return_value=False):
            assert _react_eyes_once(backend, _Unit([row])) is False
        assert backend.reactions == []


class TestReactEyesRetriesOnFailedSideEffect:
    """#1880: claim -> react -> release-on-failure, so a failed :eyes: retries."""

    def test_failed_react_rolls_back_the_receipt(self) -> None:
        row = _row("thanks")
        backend = FailingReactBackend()

        with pytest.raises(RuntimeError, match="slack react failed"):
            _react_eyes_once(backend, _Unit([row]))

        row.refresh_from_db()
        assert row.eyes_reacted_at is None  # not stamped -> next cycle retries

    def test_successful_react_stamps_exactly_once(self) -> None:
        row = _row("thanks")
        backend = RecordingBackend()

        assert _react_eyes_once(backend, _Unit([row])) is True
        row.refresh_from_db()
        assert row.eyes_reacted_at is not None
        assert backend.reactions == [("C1", "1.0", "eyes")]

    def test_two_concurrent_attempts_react_exactly_once(self) -> None:
        row_a = _row("thanks")
        row_b = PendingChatInjection.objects.get(pk=row_a.pk)
        backend_a = RecordingBackend()
        backend_b = RecordingBackend()

        first = _react_eyes_once(backend_a, _Unit([row_a]))
        second = _react_eyes_once(backend_b, _Unit([row_b]))

        assert (first, second) == (True, False)
        assert backend_a.reactions == [("C1", "1.0", "eyes")]
        assert backend_b.reactions == []


class TestHandleAckCasLost:
    def test_cas_lost_skips_reaction(self) -> None:
        row = _row("thanks")
        backend = RecordingBackend()
        with patch.object(type(row), "mark_loop_replied", return_value=False):
            assert _handle_ack(backend, _Unit([row])) is False
        assert backend.reactions == []


class TestHandleAckRetriesOnFailedSideEffect:
    """#1880: ack claims the loop-reply, then reacts; a failed react rolls back."""

    def test_failed_react_rolls_back_the_whole_unit(self) -> None:
        lead = _row("thanks", ts="1.0")
        follow = _row("thanks again", ts="1.1")
        backend = FailingReactBackend()

        with pytest.raises(RuntimeError, match="slack react failed"):
            _handle_ack(backend, _Unit([lead, follow]))

        lead.refresh_from_db()
        follow.refresh_from_db()
        assert lead.loop_replied_at is None
        assert lead.answer_kind == ""
        assert follow.loop_replied_at is None

    def test_successful_react_stamps_the_unit_once(self) -> None:
        row = _row("thanks")
        backend = RecordingBackend()

        assert _handle_ack(backend, _Unit([row])) is True
        row.refresh_from_db()
        assert row.loop_replied_at is not None
        assert row.answer_kind == "ack"
        assert backend.reactions == [("C1", "1.0", "white_check_mark")]


class TestDelegateCasLost:
    def test_cas_lost_creates_no_task(self) -> None:
        row = _row("fix the build")
        backend = RecordingBackend()
        with patch.object(type(row), "mark_loop_replied", return_value=False):
            assert _delegate_needs_work(backend, _Unit([row])) is False
        assert Task.objects.filter(phase="answering").count() == 0


class TestSimpleStageBBailFallsThroughToDelegate:
    def test_stage_b_sentinel_delegates(self) -> None:
        row = _row("which PRs are open?")
        backend = RecordingBackend()
        report = SlackAnswerReport()

        with patch(
            "teatree.loop.slack_answer.cycle.build_simple_answer",
            return_value="NEEDS_WORK",
        ):
            _process_unit(backend, _Unit([row]), report)

        assert report.delegated == 1
        assert Task.objects.filter(phase="answering").count() == 1

    def test_stage_a_none_budget_closed_delegates(self) -> None:
        row = _row("what's the status?")
        backend = RecordingBackend()
        report = SlackAnswerReport()

        with patch(
            "teatree.loop.slack_answer.cycle.build_simple_answer",
            return_value=None,
        ):
            _process_unit(backend, _Unit([row]), report)

        assert report.delegated == 1


class TestProcessUnitDegenerateBranches:
    """The three CAS-lost / already-acked fall-through arcs in ``_process_unit``."""

    def test_eyes_already_reacted_does_not_bump_counter_but_continues(self) -> None:
        # _react_eyes_once → False (already reacted): the eyes counter is
        # NOT bumped, yet classification + answering still proceed.
        row = _row("thanks")
        row.mark_eyes_reacted()
        backend = RecordingBackend()
        report = SlackAnswerReport()

        _process_unit(backend, _Unit([row]), report)

        assert report.eyes_reacted == 0
        assert report.acked == 1
        assert backend.reactions == [("C1", "1.0", "white_check_mark")]

    def test_ack_cas_lost_returns_without_counting(self) -> None:
        # ACK route but _handle_ack → False (a concurrent cycle won the
        # CAS): nothing is counted and we return before delegation.
        row = _row("thanks")
        backend = RecordingBackend()
        report = SlackAnswerReport()

        with patch.object(type(row), "mark_loop_replied", return_value=False):
            _process_unit(backend, _Unit([row]), report)

        assert report.acked == 0
        assert report.delegated == 0

    def test_delegate_cas_lost_does_not_count_delegated(self) -> None:
        # NEEDS_WORK route but _delegate_needs_work → False (CAS lost):
        # the delegated counter stays at zero.
        row = _row("fix the build")
        backend = RecordingBackend()
        report = SlackAnswerReport()

        with patch.object(type(row), "mark_loop_replied", return_value=False):
            _process_unit(backend, _Unit([row]), report)

        assert report.delegated == 0
        assert Task.objects.filter(phase="answering").count() == 0


class TestNoBackendSkip:
    def test_none_backend_is_skipped_not_an_error(self) -> None:
        _row("thanks")
        report = run_slack_answer_cycle(messaging_resolver=lambda _o: None)

        assert report.skipped_no_backend == 1
        assert report.errors == 0
        assert report.processed == 1


class TestDefaultResolver:
    def test_default_resolver_delegates_to_factory(self) -> None:
        sentinel = object()
        with patch(
            "teatree.core.backend_factory.messaging_from_overlay",
            return_value=sentinel,
        ) as factory:
            assert _default_resolver("acme") is sentinel
        factory.assert_called_once_with("acme")

    def test_default_resolver_empty_overlay_passes_none(self) -> None:
        with patch(
            "teatree.core.backend_factory.messaging_from_overlay",
            return_value=None,
        ) as factory:
            assert _default_resolver("") is None
        factory.assert_called_once_with(None)
