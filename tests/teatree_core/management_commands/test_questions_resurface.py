"""Tests for ``t3 <overlay> questions resurface`` (#182).

Returning from away must re-surface the pending ``DeferredQuestion``
backlog to the user's Slack DM — otherwise away-deferred questions are
silently swallowed (the user reads Slack, not ``t3 questions list``).
The drain reuses the canonical :func:`teatree.core.notify.notify_user`
egress (idempotent ``BotPing`` ledger, per-overlay bot routing); only
the unstoppable Slack HTTP boundary is mocked.
"""

import json
import os
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

from teatree.core.models import BotPing
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.notify_question_drains import _resurface_text

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _backend() -> MagicMock:
    b = MagicMock()
    b.open_dm.return_value = "D-USER"
    b.post_message.return_value = {"ok": True, "ts": "1700000000.000000"}
    b.get_permalink.return_value = "https://acme.slack.com/archives/D-USER/p1700000000000000"
    return b


def _call(*args: str) -> tuple[str, int]:
    buf = StringIO()
    code = 0
    try:
        call_command(*args, stdout=buf)
    except SystemExit as exc:
        code = int(exc.code or 0)
    return buf.getvalue(), code


class TestResurfaceDrainsPending:
    def test_reposts_each_pending_question_to_slack(self) -> None:
        DeferredQuestion.record("First?", session_id="s-1")
        DeferredQuestion.record("Second?", session_id="s-2")
        backend = _backend()
        with patch("teatree.core.notify.messaging_from_overlay", return_value=backend):
            out, code = _call("questions", "resurface", "--user-id", "U_ME")
        assert code == 0
        posted = [c.kwargs["text"] for c in backend.post_message.call_args_list]
        assert any("First?" in t for t in posted)
        assert any("Second?" in t for t in posted)
        assert "2" in out

    def test_skips_answered_and_dismissed(self) -> None:
        pending = DeferredQuestion.record("Pending?", session_id="s-1")
        answered = DeferredQuestion.record("Answered?", session_id="s-2")
        DeferredQuestion.consume(answered.pk, answer="done")
        backend = _backend()
        with patch("teatree.core.notify.messaging_from_overlay", return_value=backend):
            _out, code = _call("questions", "resurface", "--user-id", "U_ME")
        assert code == 0
        posted = [c.kwargs["text"] for c in backend.post_message.call_args_list]
        assert any("Pending?" in t for t in posted)
        assert not any("Answered?" in t for t in posted)
        assert any(f"#{pending.pk}" in t for t in posted)

    def test_idempotent_across_two_runs(self) -> None:
        DeferredQuestion.record("Once?", session_id="s-1")
        backend = _backend()
        with patch("teatree.core.notify.messaging_from_overlay", return_value=backend):
            _call("questions", "resurface", "--user-id", "U_ME")
            _call("questions", "resurface", "--user-id", "U_ME")
        assert backend.post_message.call_count == 1
        assert BotPing.objects.filter(kind=BotPing.Kind.QUESTION).count() == 1

    def test_empty_backlog_reports_nothing_and_exits_zero(self) -> None:
        backend = _backend()
        with patch("teatree.core.notify.messaging_from_overlay", return_value=backend):
            out, code = _call("questions", "resurface", "--user-id", "U_ME")
        assert code == 0
        backend.post_message.assert_not_called()
        assert "no" in out.lower()

    def test_slack_failure_is_swallowed_and_exits_zero(self) -> None:
        DeferredQuestion.record("Fails?", session_id="s-1")
        with patch("teatree.core.notify.messaging_from_overlay", return_value=None):
            _out, code = _call("questions", "resurface", "--user-id", "U_ME")
        assert code == 0
        assert BotPing.objects.get(kind=BotPing.Kind.QUESTION).status == BotPing.Status.NOOP


class TestOverlayRouting:
    def test_overlay_flag_sets_env_for_bot_routing(self) -> None:
        DeferredQuestion.record("Routed?", session_id="s-1")
        backend = _backend()
        seen: dict[str, str] = {}

        def _capture() -> MagicMock:
            seen["overlay"] = os.environ.get("T3_OVERLAY_NAME", "")
            return backend

        with patch("teatree.core.notify.messaging_from_overlay", side_effect=_capture):
            _call("questions", "resurface", "--user-id", "U_ME", "--overlay", "teatree")
        assert seen["overlay"] == "teatree"

    def test_overlay_flag_restores_previous_env(self) -> None:
        DeferredQuestion.record("Routed?", session_id="s-1")
        backend = _backend()
        os.environ["T3_OVERLAY_NAME"] = "pre-existing"
        try:
            with patch("teatree.core.notify.messaging_from_overlay", return_value=backend):
                _call("questions", "resurface", "--user-id", "U_ME", "--overlay", "teatree")
            assert os.environ["T3_OVERLAY_NAME"] == "pre-existing"
        finally:
            os.environ.pop("T3_OVERLAY_NAME", None)

    def test_overlay_flag_restores_unset_env(self) -> None:
        DeferredQuestion.record("Routed?", session_id="s-1")
        backend = _backend()
        os.environ.pop("T3_OVERLAY_NAME", None)
        with patch("teatree.core.notify.messaging_from_overlay", return_value=backend):
            _call("questions", "resurface", "--user-id", "U_ME", "--overlay", "teatree")
        assert "T3_OVERLAY_NAME" not in os.environ


class TestResurfaceText:
    def test_renders_question_id_and_options(self) -> None:
        row = DeferredQuestion.record(
            "Ship?",
            options_json=json.dumps([{"label": "Yes", "description": "go"}, {"label": "No"}]),
            session_id="s-1",
        )
        text = _resurface_text(row)
        assert f"#{row.pk}" in text
        assert "Ship?" in text
        assert "Yes — go" in text
        assert "No" in text
        assert f"questions answer {row.pk}" in text

    def test_malformed_options_json_is_tolerated(self) -> None:
        row = DeferredQuestion.record("Broken?", options_json="{not json", session_id="s-1")
        text = _resurface_text(row)
        assert "Broken?" in text

    def test_non_dict_option_entries_skipped(self) -> None:
        row = DeferredQuestion.record("Mixed?", options_json=json.dumps(["bare", {"label": "Real"}]), session_id="s-1")
        text = _resurface_text(row)
        assert "Real" in text
