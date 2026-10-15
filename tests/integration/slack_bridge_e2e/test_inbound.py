"""Inbound bridge: a DM Slack holds → the downtime catch-up → a PendingChatInjection row for the answer loop."""

import time

import pytest
from django.db import IntegrityError, transaction

from teatree.backends.slack.bot import SlackBotBackend
from teatree.core.models import PendingChatInjection
from teatree.loops.slack_inbound import catch_up_on_connect
from tests.integration.slack_bridge_e2e.conftest import FakeSlackTransport, serve_history

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = [pytest.mark.django_db, pytest.mark.integration]


class TestInboundBridgeEndToEnd:
    """The real catch-up over the fake transport, then the real recorder."""

    def test_dm_lands_as_pending_chat_injection_row(
        self, transport: FakeSlackTransport, overlay_backend: SlackBotBackend
    ) -> None:
        _ = overlay_backend
        ts = f"{time.time() - 30:.6f}"
        serve_history(transport, [{"ts": ts, "user": "U_HUMAN", "text": "ship PR 42"}])

        catch_up_on_connect("demo", time.time() - 120)

        row = PendingChatInjection.objects.get()
        assert (row.overlay, row.slack_ts, row.text, row.user_id, row.channel) == (
            "demo",
            ts,
            "ship PR 42",
            "U_HUMAN",
            "D-USER",
        )

    def test_thread_root_and_reply_both_land(
        self, transport: FakeSlackTransport, overlay_backend: SlackBotBackend
    ) -> None:
        _ = overlay_backend
        root, reply = f"{time.time() - 60:.6f}", f"{time.time() - 30:.6f}"
        serve_history(
            transport,
            [{"ts": root, "user": "U_HUMAN", "text": "top-level question"}],
            replies={root: [{"ts": reply, "user": "U_HUMAN", "text": "follow-up in thread"}]},
        )

        catch_up_on_connect("demo", time.time() - 120)

        rows = PendingChatInjection.objects.order_by("slack_ts").values_list("text", "thread_ts")
        assert list(rows) == [("top-level question", ""), ("follow-up in thread", root)]

    def test_double_unique_constraint_per_overlay_slack_ts(self) -> None:
        """RED if the ``uniq_pendingchat_overlay_ts`` constraint is dropped."""
        PendingChatInjection.objects.create(overlay="demo", channel="D-USER", slack_ts="dup", text="first")
        with pytest.raises(IntegrityError), transaction.atomic():
            PendingChatInjection.objects.create(overlay="demo", channel="D-USER", slack_ts="dup", text="second")
