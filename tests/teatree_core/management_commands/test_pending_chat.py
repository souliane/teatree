"""Tests for ``t3 <overlay> pending_chat`` management command (#1063)."""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from teatree.core.models import PendingChatInjection

# ast-grep-ignore: ac-django-no-pytest-django-db
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

    def test_stamps_every_row_sharing_the_ts(self) -> None:
        """The stamp keys on ``slack_ts`` alone — it does not narrow by overlay.

        Two rows sharing a ``ts`` are the same user DM recorded under two
        overlays — answering it stamps both. The old exact-overlay filter
        left the other row unanswered so the gate nagged forever.
        """
        PendingChatInjection.record(channel="D", slack_ts="ts-x", text="why?", overlay="ovA")
        PendingChatInjection.record(channel="D", slack_ts="ts-x", text="why?", overlay="ovB")

        out = _call("pending_chat", "mark-answered", "ts-x")

        assert "stamped 2 row" in out
        assert PendingChatInjection.objects.get(overlay="ovA").answered_at is not None
        assert PendingChatInjection.objects.get(overlay="ovB").answered_at is not None

    def test_cross_overlay_answer_clears_gate(self) -> None:
        """Sub-case (b): answering a row recorded under a different overlay clears it.

        The recording overlay (``overlay-alpha``) and the answering
        session's overlay differ — the common case in a concurrent
        multi-overlay deployment. The old exact-overlay filter stamped 0
        rows; the ts-keyed stamp clears the row regardless.
        """
        PendingChatInjection.record(channel="D", slack_ts="ts-cross", text="why?", overlay="overlay-alpha")

        out = _call("pending_chat", "mark-answered", "ts-cross")

        assert "stamped 1 row" in out
        assert PendingChatInjection.objects.get().answered_at is not None

    def test_empty_ts_exits_non_zero(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            _call("pending_chat", "mark-answered", "   ")

        assert excinfo.value.code == 2

    def test_bare_invocation_clears_row_recorded_under_other_overlay(self) -> None:
        """The bare ``mark-answered <ts>`` clears any matching ``ts``.

        This is what the agent runs after a direct reply. It must clear the
        row regardless of which overlay recorded it — ``slack_ts`` is the
        unique idempotency key and the stamp keys on it alone, symmetric
        with the unscoped gate.
        """
        PendingChatInjection.record(channel="D", slack_ts="ts-bare", text="why?", overlay="overlay-alpha")

        out = _call("pending_chat", "mark-answered", "ts-bare")

        assert "stamped 1 row" in out
        assert PendingChatInjection.objects.get().answered_at is not None

    def test_overlay_env_var_cannot_strand_the_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A set ``T3_OVERLAY_NAME`` differing from the recording overlay still clears.

        Regression guard for the original defect: the CLI env-fallback used
        to forward a non-empty overlay into a scoped filter, stamping 0 rows
        whenever the answering session's overlay differed from the recording
        overlay. The stamp ignores overlay entirely now, so a differing
        ``T3_OVERLAY_NAME`` cannot strand the row.
        """
        PendingChatInjection.record(channel="D", slack_ts="ts-env", text="why?", overlay="overlay-alpha")
        monkeypatch.setenv("T3_OVERLAY_NAME", "overlay-beta")

        out = _call("pending_chat", "mark-answered", "ts-env")

        assert "stamped 1 row" in out
        assert PendingChatInjection.objects.get().answered_at is not None
