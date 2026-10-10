"""The per-question re-ask bump — posted where the answer can land.

The backlog nag already recurred: a digest per 24h bucket naming ten of 147 rows.
Every one of those names was a question asked in a place its answer could not
reach — a reply under the digest carries the digest's thread ts, which joins no
question, and above one pending row the sole-live-question rung refuses to guess.

The bump is the answerable half. It rides the row and the mirror thread the
question ALREADY has, under a key carrying the row's own step on a schedule of
hours counted from its first post (1, 2, 4, 7, 12 …), and it writes no
DeferredQuestion at all — which is the property most of these pin, because the
obvious implementation silently does nothing: ``DeferredQuestion.record`` returns
the EXISTING pending row for a dedupe marker (a pending row IS the mute), so a
re-ask built on ``record()`` posts no message and looks implemented.
"""

import datetime as dt
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from teatree.core import notify as notify_module
from teatree.core.models import BotPing, DeferredQuestion
from teatree.core.notify_question_drains import _REASK_BATCH, reask_escalated_questions
from tests._owner_channel import OWNER_DECISION

_CHANNEL = "D-USER"


def _backend(*, ts: str = "1800000000.000000") -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = _CHANNEL
    backend.post_message.return_value = {"ok": True, "ts": ts}
    backend.get_permalink.return_value = "https://acme.slack.com/archives/D-USER/p1800000000000000"
    return backend


def _mirrored(question: str, *, hours_ago: float = 3, escalated: bool = False) -> DeferredQuestion:
    """A pending owner question first posted *hours_ago* hours before now, its Slack ts that instant."""
    posted_at = timezone.now() - dt.timedelta(hours=hours_ago)
    row = DeferredQuestion.record(
        question, session_id="s", slack_channel=_CHANNEL, slack_ts=f"{posted_at.timestamp():.6f}", **OWNER_DECISION
    )
    DeferredQuestion.objects.filter(pk=row.pk).update(created_at=posted_at)
    if escalated:
        row.mark_escalated("pending past the ceiling")
    row.refresh_from_db()
    return row


def _bump(backend: MagicMock, *, now: dt.datetime | None = None) -> tuple[int, int]:
    with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
        return reask_escalated_questions(user_id="U_ME", backend=backend, now=now)


class TestTheBumpRidesTheExistingRow(TestCase):
    def test_re_asking_records_no_new_question(self) -> None:
        row = _mirrored("Which DB host?")
        before = DeferredQuestion.objects.count()
        slack_ts = row.slack_ts

        assert _bump(_backend()) == (1, 1)

        assert DeferredQuestion.objects.count() == before, "the re-ask minted a row instead of riding the one it has"
        row.refresh_from_db()
        assert row.is_pending
        assert row.slack_ts == slack_ts, "the re-ask overwrote the mirror identity a reply binds on"

    def test_the_bump_lands_in_the_questions_own_thread(self) -> None:
        row = _mirrored("Which DB host?")
        backend = _backend()

        _bump(backend)

        backend.post_message.assert_called_once()
        assert backend.post_message.call_args.kwargs["thread_ts"] == row.slack_ts

    def test_the_idempotency_key_carries_the_rows_own_step_on_its_schedule(self) -> None:
        # Keyed on the ROW's position in its own schedule (hours since ITS first post), not a wall-clock
        # window every question shares — which is what lets the gaps widen per question.
        row = _mirrored("Which DB host?", hours_ago=4)

        _bump(_backend())

        assert BotPing.objects.filter(
            idempotency_key=f"reask:{row.stable_notify_ref}:3", status=BotPing.Status.SENT
        ).exists()

    def test_an_unmirrored_row_is_not_a_candidate(self) -> None:
        # No thread to bump into. The first post is the mirror drain's job — it posts at
        # root and stamps the ts this function then rides.
        DeferredQuestion.record("Which DB host?", session_id="s", **OWNER_DECISION)
        backend = _backend()

        assert _bump(backend) == (0, 0)

        backend.post_message.assert_not_called()

    def test_internal_rows_never_reach_the_owner(self) -> None:
        DeferredQuestion.record(
            "I lack the shell tool to proceed.", session_id="s", slack_channel=_CHANNEL, slack_ts="100.0"
        )

        assert _bump(_backend()) == (0, 0)

    def test_a_malformed_slack_ts_anchors_on_the_rows_creation(self) -> None:
        row = DeferredQuestion.record(
            "Which DB host?", session_id="s", slack_channel=_CHANNEL, slack_ts="not-a-ts", **OWNER_DECISION
        )
        DeferredQuestion.objects.filter(pk=row.pk).update(created_at=timezone.now() - dt.timedelta(hours=3))

        assert _bump(_backend()) == (1, 1)


class TestTheGapIsTheCadence(TestCase):
    def test_owner_bumps_follow_fibonacci_hours_from_the_first_post(self) -> None:
        row = _mirrored("Which DB host?", hours_ago=0)
        first_post = timezone.now()
        backend = _backend()
        schedule = [
            (dt.timedelta(minutes=59), 0),
            (dt.timedelta(hours=1), 1),
            (dt.timedelta(minutes=90), 0),
            (dt.timedelta(hours=2), 1),
            (dt.timedelta(hours=3), 0),
            (dt.timedelta(hours=4), 1),
            (dt.timedelta(hours=6), 0),
            (dt.timedelta(hours=7), 1),
            (dt.timedelta(hours=11), 0),
            (dt.timedelta(hours=12), 1),
        ]

        for after, expected in schedule:
            bumped, _ = _bump(backend, now=first_post + after)
            assert bumped == expected, after
            BotPing.objects.update(posted_at=timezone.now() - dt.timedelta(hours=2))

        assert backend.post_message.call_count == 5
        assert {call.kwargs["thread_ts"] for call in backend.post_message.call_args_list} == {row.slack_ts}

    def test_a_second_tick_inside_the_same_gap_posts_nothing(self) -> None:
        _mirrored("Which DB host?", hours_ago=1.5)
        now = timezone.now()
        backend = _backend()

        first, _ = _bump(backend, now=now)
        second, _ = _bump(backend, now=now + dt.timedelta(minutes=5))

        assert (first, second) == (1, 0)
        assert backend.post_message.call_count == 1, "every tick re-bumped the owner"

    def test_a_freshly_mirrored_row_is_not_bumped_on_top_of_its_first_post(self) -> None:
        # The mirror drain has just posted it; step 0 IS that post.
        _mirrored("Which DB host?", hours_ago=0)
        backend = _backend()

        assert _bump(backend) == (0, 1)

        backend.post_message.assert_not_called()

    def test_a_first_post_held_overnight_is_not_bumped_right_after_it_goes_out(self) -> None:
        # Created at 23:00, first posted at 08:00: the clock starts at the POST, not at the row.
        eight = timezone.now().replace(hour=8, minute=0, second=0, microsecond=0)
        row = DeferredQuestion.record(
            "Which DB host?",
            session_id="s",
            slack_channel=_CHANNEL,
            slack_ts=f"{eight.timestamp():.6f}",
            **OWNER_DECISION,
        )
        DeferredQuestion.objects.filter(pk=row.pk).update(created_at=eight - dt.timedelta(hours=9))
        backend = _backend()

        assert _bump(backend, now=eight + dt.timedelta(minutes=1)) == (0, 1)
        assert _bump(backend, now=eight + dt.timedelta(hours=1)) == (1, 1)

    def test_the_gaps_widen_so_a_stale_question_costs_less_than_a_fresh_one(self) -> None:
        # A question 30 hours old and one 33 hours old are on adjacent steps of the schedule.
        young = _mirrored("Young?", hours_ago=33)
        old = _mirrored("Old?", hours_ago=54)

        _bump(_backend())

        keys = set(BotPing.objects.values_list("idempotency_key", flat=True))
        assert keys == {f"reask:{young.stable_notify_ref}:7", f"reask:{old.stable_notify_ref}:8"}

    def test_the_batch_rotates_through_the_backlog(self) -> None:
        # The five slots went to the five most urgent rows every pass, so row six
        # was never bumped at all while rows one to five were bumped every time.
        for i in range(_REASK_BATCH * 2):
            _mirrored(f"Widget {i}?", hours_ago=40 - i)
        backend = _backend()

        _bump(backend)
        # The second pass runs in a later hour, past the owner's hourly question-ping ceiling.
        BotPing.objects.update(posted_at=timezone.now() - dt.timedelta(hours=2))
        _bump(backend)

        threads = {call.kwargs["thread_ts"] for call in backend.post_message.call_args_list}
        assert len(threads) == _REASK_BATCH * 2, "the second pass re-bumped the same five"


class TestTheBatchIsBoundedAndUrgentFirst(TestCase):
    def test_only_the_batch_size_is_bumped_per_pass(self) -> None:
        for i in range(_REASK_BATCH + 4):
            _mirrored(f"Widget {i}?")

        bumped, candidates = _bump(_backend())

        assert bumped == _REASK_BATCH
        assert candidates == _REASK_BATCH + 4, "the candidate count must report the backlog, not the slice"

    def test_escalated_rows_are_bumped_before_younger_ones(self) -> None:
        fresh = [_mirrored(f"Fresh {i}?", hours_ago=1.5) for i in range(_REASK_BATCH)]
        stale = _mirrored("Stale?", hours_ago=40, escalated=True)
        backend = _backend()

        _bump(backend)

        threads = [call.kwargs["thread_ts"] for call in backend.post_message.call_args_list]
        assert stale.slack_ts in threads, "the row past the age ceiling was crowded out by younger ones"
        assert fresh[-1].slack_ts not in threads

    def test_the_bump_says_how_long_the_question_has_waited_in_plain_words(self) -> None:
        _mirrored("Which DB host?", hours_ago=12, escalated=True)
        backend = _backend()

        _bump(backend)

        text = backend.post_message.call_args.kwargs["text"]
        assert text == "Still waiting for your decision; I asked 12 hours ago. Tap an option above, or reply here."
