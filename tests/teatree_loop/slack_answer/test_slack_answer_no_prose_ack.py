"""Regression for #1155: the delegated path posts NO prose ack.

The user reads Slack DMs only and treats every thread reply as a
notification on their phone. The :eyes: receipt reaction already signals
"received". A second arrival that says "On it — investigating, I'll
follow up here." is pure noise — it does not carry information the
user does not already have from the :eyes:.

This module pins two claims for the NEEDS_WORK / delegated path:

- The cycle does NOT call ``post_reply`` on the delegated path
    (no prose ack, no "On it" prose, nothing — the thread stays clean).
- The cycle DOES react :eyes: on the same row (the existing-path
    acknowledgement mechanism must keep firing).

The fix in ``cycle.py`` deletes ``_INSTANT_ACK_TEXT`` and its
``backend.post_reply(...)`` call site; the :eyes: react in
``_react_eyes_once`` is untouched. Reverting the deletion makes the
"no prose post" assertion RED — the anti-vacuousness check this
module's bug fix relies on.
"""

from dataclasses import dataclass, field

import pytest

from teatree.core.models import PendingChatInjection, Task
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from teatree.types import RawAPIDict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@dataclass
class RecordingBackend:
    """In-memory MessagingBackend recording react / post_reply calls."""

    reactions: list[tuple[str, str, str]] = field(default_factory=list)
    replies: list[tuple[str, str, str]] = field(default_factory=list)

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        self.replies.append((channel, ts, text))
        return {"ok": True}

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return "D1"

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = (channel, ts)
        return "https://slack/p1"

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.reactions.append((channel, ts, emoji))
        return {"ok": True}

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""


def _row(text: str, ts: str = "1.0", overlay: str = "") -> PendingChatInjection:
    row = PendingChatInjection.record(channel="C1", slack_ts=ts, text=text, overlay=overlay)
    assert row is not None
    return row


def _resolver(backend: RecordingBackend):
    return lambda _overlay: backend


class TestDelegatedPathHasNoProseAck:
    """The NEEDS_WORK / delegated path must post NO prose ack message."""

    def test_no_post_reply_on_delegated_path(self) -> None:
        """The cycle must not call ``post_reply`` at all on the delegated path.

        The :eyes: reaction is the only acknowledgement; a thread post
        with prose like "On it — investigating, I'll follow up here."
        is noise the user does not want to receive.
        """
        _row("fix the failing pipeline")
        backend = RecordingBackend()

        report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert report.delegated == 1
        # The delegated path creates the Task but posts NO thread reply.
        assert backend.replies == [], f"Expected no thread replies on the delegated path; got: {backend.replies!r}"

    def test_no_on_it_prose_in_any_reply(self) -> None:
        """Defence-in-depth: no reply contains the 'On it' prose text.

        If a future regression reintroduces an instant-ack message under
        a different name, this guard catches the prose substring.
        """
        _row("investigate the flaky test")
        backend = RecordingBackend()

        run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        for _channel, _ts, text in backend.replies:
            assert "On it" not in text, f"Unexpected 'On it' prose in reply: {text!r}"
            assert "investigating" not in text.lower(), f"Unexpected 'investigating' prose in reply: {text!r}"

    def test_eyes_react_still_fires_on_delegated_path(self) -> None:
        """The acknowledgement mechanism (:eyes: react) MUST keep working.

        Removing the prose post must not regress the :eyes: receipt —
        that is the load-bearing "received" signal to the user.
        """
        row = _row("fix the failing pipeline")
        backend = RecordingBackend()

        run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        eyes = [r for r in backend.reactions if r[2] == "eyes"]
        assert len(eyes) == 1, f"Expected exactly one :eyes: react; got: {backend.reactions!r}"
        assert eyes[0][0] == "C1"
        assert eyes[0][1] == "1.0"
        row.refresh_from_db()
        assert row.eyes_reacted_at is not None

    def test_delegation_still_creates_the_answerer_task(self) -> None:
        """Removing the prose post must not regress Task creation.

        The whole point of the delegated path is to spawn the answerer
        sub-agent via a PENDING Task — removing the prose ack must not
        accidentally short-circuit that side effect.
        """
        _row("fix the failing pipeline")
        backend = RecordingBackend()

        run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        tasks = Task.objects.filter(phase="answering", status=Task.Status.PENDING)
        assert tasks.count() == 1
