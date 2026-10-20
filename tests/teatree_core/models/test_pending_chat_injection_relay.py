"""The terminal relay: a verbatim ask posted into the owner's factory DM and queued like a typed DM.

Real manager, real egress, real ``BotPing`` ledger; only the Slack DM, the clock-free heartbeat file and
the transport registry are faked (``tests/_factory_slack.py``).
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.modelkit.relay import RelayReceipt, RelayRefusal
from teatree.core.models import BotPing, PendingChatInjection
from teatree.core.notify import NotifyKind, notify_user
from tests._factory_slack import OWNER_ID, SLACK_OVERLAY, FakeOwnerDm, live_factory_slack

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
