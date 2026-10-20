# test-path: cross-cutting
# The relay is a second PendingChatInjection writer; this drives it through the recorder every inbound DM passes.
"""The bot's own relay post, delivered back as a Socket Mode event, never becomes a second row."""

from teatree.core.models import PendingChatInjection
from teatree.loop.scanners.slack_dm_recorder import SlackDmRecorder
from tests._factory_slack import BOT_ID, OWNER_ID, SLACK_OVERLAY, FakeOwnerDm, live_factory_slack


def test_bot_relay_post_is_not_queued_twice(db: None) -> None:
    dm = FakeOwnerDm()
    with live_factory_slack(dm):
        receipt = PendingChatInjection.objects.relay_attended_ask(SLACK_OVERLAY, "ship it?", "sess-1", relay_id="r1")
    [post] = dm.posts
    recorder = SlackDmRecorder(backend=dm, overlay=SLACK_OVERLAY)

    own_post_event = {"ts": post["ts"], "user": BOT_ID, "channel": dm.channel, "text": post["text"]}
    owner_typed_event = {"ts": "1700000099.000001", "user": OWNER_ID, "channel": dm.channel, "text": "and the docs?"}

    assert recorder.record_events([own_post_event]) == 0
    assert PendingChatInjection.objects.count() == 1
    assert recorder.record_events([owner_typed_event]) == 1
    assert set(PendingChatInjection.objects.values_list("pk", flat=True)) >= {receipt.pk}
    assert PendingChatInjection.objects.count() == 2
