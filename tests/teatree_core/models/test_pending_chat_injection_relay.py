"""The terminal relay: a verbatim ask posted into the owner's factory DM and queued like a typed DM.

Real manager, real egress, real ``BotPing`` ledger; only the Slack DM, the clock-free heartbeat file and
the transport registry are faked (``tests/_factory_slack.py``).
"""

import datetime as dt
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.modelkit.relay import RelayReceipt, RelayRefusal
from teatree.core.models import BotPing, DmContext, PendingChatInjection, Task
from teatree.core.notify import NotifyKind, notify_user
from teatree.utils.url_slug import slack_conversation_anchor
from tests._factory_slack import OWNER_DM, OWNER_ID, SLACK_OVERLAY, FakeOwnerDm, live_factory_slack
from tests.factories import TaskFactory, TicketFactory

PROMPT = "  Why is *this* red?\nsecond line  \n"


def _relay(prompt: str = PROMPT, *, relay_id: str = "r1", session: str = "sess-1") -> RelayReceipt | RelayRefusal:
    return PendingChatInjection.objects.relay_attended_ask(SLACK_OVERLAY, prompt, session, relay_id=relay_id)


class TestRelayAttendedAsk(TestCase):
    def test_relay_required_when_factory_slack_live(self) -> None:
        with live_factory_slack(FakeOwnerDm()):
            assert PendingChatInjection.objects.relay_required(SLACK_OVERLAY) is True

    def test_relay_refused_with_no_row_and_no_post_when_slack_not_live(self) -> None:
        dm = FakeOwnerDm()

        refusal = _relay()

        assert isinstance(refusal, RelayRefusal)
        assert not refusal.delivery_failed
        assert PendingChatInjection.objects.relay_required(SLACK_OVERLAY) is False
        assert PendingChatInjection.objects.count() == 0
        assert dm.posts == []

    def test_relayed_row_carries_configured_owner_id_and_posted_ts(self) -> None:
        dm = FakeOwnerDm()
        with live_factory_slack(dm):
            receipt = _relay()

        assert isinstance(receipt, RelayReceipt)
        [post] = dm.posts
        row = PendingChatInjection.objects.get()
        assert (row.pk, row.slack_ts, row.channel) == (receipt.pk, post["ts"], dm.channel)
        assert (row.user_id, row.overlay, row.is_relayed, row.relayed_by_session) == (
            OWNER_ID,
            SLACK_OVERLAY,
            True,
            "sess-1",
        )
        assert row.text == PROMPT
        assert post["thread_ts"] == ""
        assert post["text"].endswith(PROMPT)
        assert receipt.permalink == f"https://slack.test/archives/{dm.channel}/p{post['ts'].replace('.', '')}"

    def test_relayed_row_is_drained_by_loop_unreplied_like_a_typed_dm(self) -> None:
        with live_factory_slack(FakeOwnerDm()):
            receipt = _relay()

        assert isinstance(receipt, RelayReceipt)
        assert [row.pk for row in PendingChatInjection.loop_unreplied(overlay=SLACK_OVERLAY)] == [receipt.pk]

    def test_relay_is_idempotent_for_the_same_session_prompt(self) -> None:
        dm = FakeOwnerDm()
        with live_factory_slack(dm):
            first = _relay()
            again = _relay()

        assert isinstance(first, RelayReceipt)
        assert isinstance(again, RelayReceipt)
        assert (again.pk, again.ts, again.permalink) == (first.pk, first.ts, first.permalink)
        assert first.created
        assert not again.created
        assert len(dm.posts) == 1
        assert PendingChatInjection.objects.count() == 1


class TestRelayRefusals(TestCase):
    def test_an_unrelayable_prompt_posts_nothing_and_writes_no_row(self) -> None:
        unrelayable = {
            "blank": "  \n ",
            "38000": "x" * 38_001,
            "secret": "here is my key sk-" + "ant-api03-" + "a" * 40,
        }
        for reason, prompt in unrelayable.items():
            with self.subTest(reason=reason):
                dm = FakeOwnerDm()
                with live_factory_slack(dm):
                    refusal = _relay(prompt)

                assert isinstance(refusal, RelayRefusal)
                assert reason in refusal.reason
                assert not refusal.delivery_failed
                assert dm.posts == []
                assert PendingChatInjection.objects.count() == 0

    def test_a_delivery_that_is_not_confirmed_writes_no_row(self) -> None:
        for name, dm in {
            "post-fails": FakeOwnerDm(fail_post=True),
            "no-read-back": FakeOwnerDm(read_back=False),
        }.items():
            with self.subTest(name=name), live_factory_slack(dm):
                refusal = _relay(relay_id=name)

                assert isinstance(refusal, RelayRefusal)
                assert refusal.delivery_failed
                assert PendingChatInjection.objects.count() == 0


class TestRelayEgress(TestCase):
    def test_a_relay_post_has_no_speech_sidecar_while_an_info_dm_does(self) -> None:
        dm = FakeOwnerDm()
        with live_factory_slack(dm), patch("teatree.core.speak.deliver_user_dm_sidecar") as sidecar:
            _relay()
            relay_calls = sidecar.call_count
            notify_user(
                "an info",
                kind=NotifyKind.INFO,
                idempotency_key="info-1",
                audience=NotifyAudience.OWNER_QUESTION,
                backend=dm,
                user_id=OWNER_ID,
            )

        assert relay_calls == 0
        assert sidecar.call_count == 1

    def test_a_relay_ping_that_failed_is_never_redelivered_as_a_ghost(self) -> None:
        dm = FakeOwnerDm(fail_post=True)
        with live_factory_slack(dm):
            _relay()
            notify_user(
                "an info",
                kind=NotifyKind.INFO,
                idempotency_key="info-failed",
                audience=NotifyAudience.OWNER_QUESTION,
                backend=dm,
                user_id=OWNER_ID,
            )

        failed = BotPing.objects.filter(status=BotPing.Status.FAILED)
        assert {row.kind for row in failed} == {BotPing.Kind.RELAY, BotPing.Kind.INFO}
        assert {row.kind for row in BotPing.recoverable_info()} == {BotPing.Kind.INFO}


_NOW = timezone.now()


def _relay_row(ts: str, *, session: str | None, age_minutes: float, answered: bool = False) -> PendingChatInjection:
    row = PendingChatInjection.record(
        channel=OWNER_DM,
        slack_ts=ts,
        text=f"prompt {ts}",
        overlay=SLACK_OVERLAY,
        context=DmContext(user_id=OWNER_ID, relayed_by_session=session),
    )
    assert row is not None
    PendingChatInjection.objects.filter(pk=row.pk).update(
        received_at=_NOW - dt.timedelta(minutes=age_minutes),
        loop_response_confirmed_at=_NOW if answered else None,
    )
    return row


def _delegated(row: PendingChatInjection, status: Task.Status, *, age_minutes: float = 1) -> None:
    PendingChatInjection.objects.filter(pk=row.pk).update(answer_kind="delegated", loop_response_confirmed_at=_NOW)
    ticket = TicketFactory(issue_url=slack_conversation_anchor(channel=row.channel, slack_ts=row.slack_ts))
    task = TaskFactory(ticket=ticket, phase="answering", status=status)
    Task.objects.filter(pk=task.pk).update(created_at=_NOW - dt.timedelta(minutes=age_minutes))


class TestRelayFollowUp(TestCase):
    def test_open_relays_lists_unreplied_and_failed_delegations_for_the_session(self) -> None:
        silent = _relay_row("1.1", session="s1", age_minutes=10)
        _relay_row("1.2", session="s1", age_minutes=1)
        _relay_row("1.3", session="s1", age_minutes=10, answered=True)
        failed = _relay_row("1.4", session="s1", age_minutes=10)
        _delegated(failed, Task.Status.FAILED)
        stalled = _relay_row("1.5", session="s1", age_minutes=120)
        _delegated(stalled, Task.Status.CLAIMED, age_minutes=90)
        working = _relay_row("1.6", session="s1", age_minutes=20)
        _delegated(working, Task.Status.CLAIMED, age_minutes=5)
        done = _relay_row("1.7", session="s1", age_minutes=10)
        _delegated(done, Task.Status.COMPLETED)
        other_session = _relay_row("2.1", session="s2", age_minutes=10)
        PendingChatInjection.record(channel=OWNER_DM, slack_ts="3.1", text="typed", overlay=SLACK_OVERLAY)
        PendingChatInjection.objects.filter(slack_ts="3.1").update(received_at=_NOW - dt.timedelta(minutes=10))

        mine = PendingChatInjection.objects.open_relays("s1", now=_NOW)
        everyone = PendingChatInjection.objects.open_relays(now=_NOW)

        assert {row.pk for row in mine} == {silent.pk, failed.pk, stalled.pk}
        assert {row.pk for row in everyone} == {silent.pk, failed.pk, stalled.pk, other_session.pk}
