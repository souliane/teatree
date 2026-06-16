"""The away→present transition auto-drains the deferred backlog (#182).

The user's complaint is "when I am back from away, you don't ask me the
questions". A drain that only fires when the agent remembers to run
``t3 questions resurface`` re-introduces the agent-memory dependence that
already failed. ``write_override(MODE_PRESENT)`` — the canonical transition
point, called by ``t3 availability present`` — must auto-post the pending
backlog to the user's Slack DM, but ONLY on a real away→present transition
(idempotent, no spurious re-asks when already present) and fail-open (a
Slack failure must never block the availability flip).

The drain reuses the canonical :func:`teatree.core.notify.notify_user`
egress; only the unstoppable Slack HTTP boundary is mocked.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

from teatree.core.availability import MODE_AWAY, MODE_PRESENT, load_override, write_override
from teatree.core.models import BotPing
from teatree.core.models.deferred_question import DeferredQuestion

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@pytest.fixture
def override_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "availability_override.json"
    monkeypatch.setattr("teatree.core.availability.override_path", lambda: target)
    return target


def _backend() -> MagicMock:
    b = MagicMock()
    b.open_dm.return_value = "D-USER"
    b.post_message.return_value = {"ok": True, "ts": "1700000000.000000"}
    b.get_permalink.return_value = "https://acme.slack.com/archives/D-USER/p1700000000000000"
    return b


class TestAwayToPresentAutoDrains:
    def test_setting_present_from_away_reposts_each_pending_question(self, override_file: Path) -> None:
        write_override(MODE_AWAY)
        DeferredQuestion.record("First?", session_id="s-1")
        DeferredQuestion.record("Second?", session_id="s-2")
        backend = _backend()
        with patch("teatree.core.notify.messaging_from_overlay", return_value=backend):
            write_override(MODE_PRESENT, user_id="U_ME")
        posted = [c.kwargs["text"] for c in backend.post_message.call_args_list]
        assert any("First?" in t for t in posted)
        assert any("Second?" in t for t in posted)

    def test_present_from_present_does_not_drain(self, override_file: Path) -> None:
        write_override(MODE_PRESENT)
        DeferredQuestion.record("Latent?", session_id="s-1")
        backend = _backend()
        with patch("teatree.core.notify.messaging_from_overlay", return_value=backend):
            write_override(MODE_PRESENT, user_id="U_ME")
        backend.post_message.assert_not_called()

    def test_away_to_present_with_empty_backlog_is_a_noop(self, override_file: Path) -> None:
        write_override(MODE_AWAY)
        backend = _backend()
        with patch("teatree.core.notify.messaging_from_overlay", return_value=backend):
            write_override(MODE_PRESENT, user_id="U_ME")
        backend.post_message.assert_not_called()

    def test_slack_failure_during_drain_does_not_break_the_flip(self, override_file: Path) -> None:
        write_override(MODE_AWAY)
        DeferredQuestion.record("Fails?", session_id="s-1")
        with patch("teatree.core.notify.messaging_from_overlay", side_effect=RuntimeError("slack down")):
            result = write_override(MODE_PRESENT, user_id="U_ME")
        assert result == override_file
        loaded = load_override()
        assert loaded is not None
        assert loaded.mode == MODE_PRESENT

    def test_transition_drain_is_idempotent_with_manual_resurface(self, override_file: Path) -> None:
        write_override(MODE_AWAY)
        DeferredQuestion.record("Once?", session_id="s-1")
        backend = _backend()
        with patch("teatree.core.notify.messaging_from_overlay", return_value=backend):
            write_override(MODE_PRESENT, user_id="U_ME")
            call_command("questions", "resurface", "--user-id", "U_ME")
        assert backend.post_message.call_count == 1
        assert BotPing.objects.filter(kind=BotPing.Kind.QUESTION).count() == 1
