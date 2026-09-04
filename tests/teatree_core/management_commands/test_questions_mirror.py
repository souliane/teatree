"""``t3 <overlay> questions mirror --ref <ref>`` — the capture-time delivery kick (#4673).

The loop-driven ``AskUserQuestion`` deny arm no longer posts to Slack inline; it
records the row and spawns this command detached. It targets ONE row through the
same ``drain_unmirrored_deferred_questions`` -> ``notify_user`` chokepoint the tick
scanner uses, so there is one Slack egress rather than two.
"""

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

from teatree.core import notify as notify_module
from teatree.core.models.deferred_question import DeferredQuestion

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


class TestQuestionsMirror:
    def test_delivers_the_targeted_row_and_stamps_it(self) -> None:
        row = DeferredQuestion.record("Which env?", session_id="s-loop", tool_use_id="toolu-1")
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            out, code = _call("questions", "mirror", "--ref", row.stable_notify_ref, "--user-id", "U_ME")

        assert code == 0
        assert "mirrored" in out
        row.refresh_from_db()
        assert row.slack_ts == "1700000000.000000"
        assert row.slack_channel == "D-USER"

    def test_jumps_a_backlog_bigger_than_the_per_tick_cap(self) -> None:
        for i in range(5):
            DeferredQuestion.record(f"older #{i}", session_id="s", tool_use_id=f"older-{i}")
        fresh = DeferredQuestion.record("the blocker", session_id="s-loop", tool_use_id="toolu-fresh")
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            _out, code = _call("questions", "mirror", "--ref", fresh.stable_notify_ref, "--user-id", "U_ME")

        assert code == 0
        fresh.refresh_from_db()
        assert fresh.slack_ts != ""
        assert backend.post_message.call_count == 1

    def test_unknown_ref_reports_nothing_delivered_without_failing(self) -> None:
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            out, code = _call("questions", "mirror", "--ref", "no-such-ref", "--user-id", "U_ME")

        # Fire-and-forget from a crash-proof hook: a lost race with the tick drain
        # is not an error, and the durable row is still the fallback.
        assert code == 0
        assert "nothing to mirror" in out
        assert backend.post_message.call_count == 0

    def test_blank_ref_is_refused_rather_than_draining_the_backlog(self) -> None:
        DeferredQuestion.record("older", session_id="s", tool_use_id="older-1")
        backend = _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            _out, code = _call("questions", "mirror", "--ref", "", "--user-id", "U_ME")

        assert code == 2
        assert backend.post_message.call_count == 0
