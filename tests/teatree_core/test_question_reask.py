"""The per-question re-ask bump — posted where the answer can land.

The backlog nag already recurred: a digest per 24h bucket naming ten of 147 rows.
Every one of those names was a question asked in a place its answer could not
reach — a reply under the digest carries the digest's thread ts, which joins no
question, and above one pending row the sole-live-question rung refuses to guess.

The bump is the answerable half. It rides the row and the mirror thread the
question ALREADY has, under a 24h-bucketed idempotency key, and it writes no
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
from teatree.core.notify_question_drains import _REASK_BATCH, RESURFACE_INTERVAL_HOURS, reask_escalated_questions

_CHANNEL = "D-USER"


def _backend(*, ts: str = "1800000000.000000") -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = _CHANNEL
    backend.post_message.return_value = {"ok": True, "ts": ts}
    backend.get_permalink.return_value = "https://acme.slack.com/archives/D-USER/p1800000000000000"
    return backend


def _mirrored(question: str, *, slack_ts: str, escalated: bool = False, age_days: int = 3) -> DeferredQuestion:
    row = DeferredQuestion.record(question, session_id="s", slack_channel=_CHANNEL, slack_ts=slack_ts)
    DeferredQuestion.objects.filter(pk=row.pk).update(created_at=timezone.now() - dt.timedelta(days=age_days))
    if escalated:
        row.mark_escalated("pending past the ceiling")
    row.refresh_from_db()
    return row


class TestTheBumpRidesTheExistingRow(TestCase):
    def test_re_asking_records_no_new_question(self) -> None:
        row = _mirrored("Which DB host?", slack_ts="100.0")
        before = DeferredQuestion.objects.count()
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            bumped, candidates = reask_escalated_questions(user_id="U_ME", backend=backend)

        assert (bumped, candidates) == (1, 1)
        assert DeferredQuestion.objects.count() == before, "the re-ask minted a row instead of riding the one it has"
        row.refresh_from_db()
        assert row.is_pending
        assert row.slack_ts == "100.0", "the re-ask overwrote the mirror identity a reply binds on"

    def test_the_bump_lands_in_the_questions_own_thread(self) -> None:
        _mirrored("Which DB host?", slack_ts="100.0")
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            reask_escalated_questions(user_id="U_ME", backend=backend)

        backend.post_message.assert_called_once()
        assert backend.post_message.call_args.kwargs["thread_ts"] == "100.0"

    def test_the_idempotency_key_carries_the_interval_bucket(self) -> None:
        row = _mirrored("Which DB host?", slack_ts="100.0")
        now = timezone.now()
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            reask_escalated_questions(user_id="U_ME", backend=backend, now=now)

        bucket = int(now.timestamp()) // (RESURFACE_INTERVAL_HOURS * 3600)
        assert BotPing.objects.filter(
            idempotency_key=f"reask:{row.stable_notify_ref}:{bucket}",
            status=BotPing.Status.SENT,
        ).exists()

    def test_an_unmirrored_row_is_not_a_candidate(self) -> None:
        # No thread to bump into. The first post is the mirror drain's job — it posts at
        # root and stamps the ts this function then rides.
        DeferredQuestion.record("Which DB host?", session_id="s")
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            assert reask_escalated_questions(user_id="U_ME", backend=backend) == (0, 0)

        backend.post_message.assert_not_called()

    def test_internal_rows_never_reach_the_owner(self) -> None:
        DeferredQuestion.record(
            "I lack the shell tool to proceed.",
            session_id="s",
            slack_channel=_CHANNEL,
            slack_ts="100.0",
            audience=DeferredQuestion.Audience.INTERNAL,
        )
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            assert reask_escalated_questions(user_id="U_ME", backend=backend) == (0, 0)


class TestTheBucketIsTheCadence(TestCase):
    def test_a_second_tick_in_the_same_bucket_posts_nothing(self) -> None:
        _mirrored("Which DB host?", slack_ts="100.0")
        now = timezone.now()
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            first, _ = reask_escalated_questions(user_id="U_ME", backend=backend, now=now)
            second, _ = reask_escalated_questions(user_id="U_ME", backend=backend, now=now + dt.timedelta(minutes=5))

        assert (first, second) == (1, 0)
        assert backend.post_message.call_count == 1, "every tick inside one bucket re-bumped the owner"

    def test_the_next_bucket_bumps_again(self) -> None:
        _mirrored("Which DB host?", slack_ts="100.0")
        now = timezone.now()
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            reask_escalated_questions(user_id="U_ME", backend=backend, now=now)
            later, _ = reask_escalated_questions(
                user_id="U_ME",
                backend=backend,
                now=now + dt.timedelta(hours=RESURFACE_INTERVAL_HOURS + 1),
            )

        assert later == 1, "an unanswered question stopped being re-asked after one bucket"


class TestTheBatchIsBoundedAndUrgentFirst(TestCase):
    def test_only_the_batch_size_is_bumped_per_bucket(self) -> None:
        for i in range(_REASK_BATCH + 4):
            _mirrored(f"Question {i}?", slack_ts=f"{100 + i}.0")
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            bumped, candidates = reask_escalated_questions(user_id="U_ME", backend=backend)

        assert bumped == _REASK_BATCH
        assert candidates == _REASK_BATCH + 4, "the candidate count must report the backlog, not the slice"

    def test_escalated_rows_are_bumped_before_younger_ones(self) -> None:
        fresh = [_mirrored(f"Fresh {i}?", slack_ts=f"{200 + i}.0", age_days=1) for i in range(_REASK_BATCH)]
        stale = _mirrored("Stale?", slack_ts="300.0", escalated=True, age_days=40)
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            reask_escalated_questions(user_id="U_ME", backend=backend)

        threads = [call.kwargs["thread_ts"] for call in backend.post_message.call_args_list]
        assert stale.slack_ts in threads, "the row past the age ceiling was crowded out by younger ones"
        assert fresh[-1].slack_ts not in threads

    def test_the_bump_renders_the_age_the_backstop_stamped(self) -> None:
        _mirrored("Which DB host?", slack_ts="100.0", escalated=True, age_days=12)
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            reask_escalated_questions(user_id="U_ME", backend=backend)

        text = backend.post_message.call_args.kwargs["text"]
        assert "12d" in text
        assert "escalated 1x" in text
