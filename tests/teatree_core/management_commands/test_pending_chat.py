"""Tests for ``t3 <overlay> pending_chat`` management command (#1063)."""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from teatree.core.models import PendingChatInjection

pytestmark = pytest.mark.django_db


def _call(*args: str) -> str:
    buf = StringIO()
    call_command(*args, stdout=buf)
    return buf.getvalue()


class TestListSubcommand:
    def test_list_empty_says_no_rows(self) -> None:
        out = _call("pending_chat", "list")
        assert "no inbound rows" in out

    def test_list_default_window_only_recent(self) -> None:
        old = PendingChatInjection.record(channel="D", slack_ts="1", text="ancient")
        recent = PendingChatInjection.record(channel="D", slack_ts="2", text="fresh")
        assert old is not None
        assert recent is not None
        old.received_at = timezone.now() - timedelta(hours=5)
        old.save(update_fields=["received_at"])

        out = _call("pending_chat", "list")

        assert "fresh" in out
        assert "ancient" not in out

    def test_list_all_includes_old(self) -> None:
        old = PendingChatInjection.record(channel="D", slack_ts="1", text="ancient")
        assert old is not None
        old.received_at = timezone.now() - timedelta(hours=5)
        old.save(update_fields=["received_at"])

        out = _call("pending_chat", "list", "--all")

        assert "ancient" in out

    def test_list_marks_question_rows(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="1", text="why is this red?")

        out = _call("pending_chat", "list")

        assert "question" in out

    def test_list_marks_answered_rows(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="1", text="why?")
        PendingChatInjection.agent_answered_question("1")

        out = _call("pending_chat", "list")

        assert "answered" in out

    def test_list_marks_consumed_rows(self) -> None:
        row = PendingChatInjection.record(channel="D", slack_ts="1", text="status update")
        assert row is not None
        assert row.consume() is True

        out = _call("pending_chat", "list")

        assert "consumed" in out


class TestMarkAnsweredSubcommand:
    def test_stamps_matching_row(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="ts-x", text="why?")

        out = _call("pending_chat", "mark-answered", "ts-x")

        assert "stamped 1 row" in out
        assert PendingChatInjection.objects.get().answered_at is not None

    def test_unknown_ts_says_zero_stamped(self) -> None:
        out = _call("pending_chat", "mark-answered", "ts-not-here")

        assert "stamped 0 row" in out

    def test_second_call_is_zero_stamped(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="ts-x", text="why?")
        _call("pending_chat", "mark-answered", "ts-x")

        out = _call("pending_chat", "mark-answered", "ts-x")

        assert "stamped 0 row" in out

    def test_overlay_scoping(self) -> None:
        PendingChatInjection.record(channel="D", slack_ts="ts-x", text="why?", overlay="ovA")
        PendingChatInjection.record(channel="D", slack_ts="ts-x", text="why?", overlay="ovB")

        out = _call("pending_chat", "mark-answered", "ts-x", "--overlay", "ovA")

        assert "stamped 1 row" in out
        assert PendingChatInjection.objects.get(overlay="ovA").answered_at is not None
        assert PendingChatInjection.objects.get(overlay="ovB").answered_at is None

    def test_empty_ts_exits_non_zero(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            _call("pending_chat", "mark-answered", "   ")

        assert excinfo.value.code == 2
