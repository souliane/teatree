"""Integration test: the Slack posting chokepoint linkifies bare refs in code.

``SlackReplier._deliver`` is the user-facing send chokepoint (#654). It must
deterministically rewrite a bare ``!N`` / ``#N`` into a clickable Slack mrkdwn
link BEFORE the body leaves teatree — no model round-trip. Resolution comes
from the active overlay's ``resolve_mr_token`` / ``resolve_issue_token`` (DB
``PullRequest`` store first, then repo-context construction). An unresolvable
ref is left bare for the gate's fallback.

The Slack send itself is mocked; the assertion is on the body handed to the
backend, proving the rewrite happened in code at the chokepoint.
"""

from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.models import IncomingEvent
from teatree.core.reply_transport import SlackReplier
from tests.teatree_core._on_behalf_gate_helpers import disable_on_behalf_gate


@pytest.fixture(autouse=True)
def _no_on_behalf_gate(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    disable_on_behalf_gate(tmp_path_factory, monkeypatch)


def _event(key: str) -> IncomingEvent:
    return IncomingEvent.objects.create(
        source=IncomingEvent.Source.SLACK,
        actor="U_ALICE",
        channel_ref="C-eng",
        thread_ref="1700000000.0001",
        body="hi",
        idempotency_key=key,
    )


class TestSlackReplierLinkifies(TestCase):
    def _replier_with_capture(self) -> tuple[SlackReplier, MagicMock]:
        bot = MagicMock()
        bot.post_message.return_value = {"ok": True, "ts": "1700000000.0002"}
        bot.get_permalink.return_value = "https://slack.example.com/p1"
        return SlackReplier(bot=bot), bot

    def test_resolvable_ref_becomes_clickable_link(self) -> None:
        replier, bot = self._replier_with_capture()
        # Wire a deterministic resolver onto the active overlay so the
        # chokepoint resolves !281 -> a URL with no model involvement.
        with self._overlay_resolving({281: "https://github.com/acme/widgets/pull/281"}, {}):
            replier.post_in_thread(
                event=_event("slack-linkify-1"),
                target_ref="C-eng",
                thread_ref="1700000000.0001",
                body="please review !281 today",
                idempotency_key="slack-linkify-1",
            )
        sent = bot.post_message.call_args.kwargs["text"]
        assert "<https://github.com/acme/widgets/pull/281|!281>" in sent

    def test_unresolvable_ref_left_bare(self) -> None:
        replier, bot = self._replier_with_capture()
        with self._overlay_resolving({}, {}):
            replier.post_in_thread(
                event=_event("slack-linkify-2"),
                target_ref="C-eng",
                thread_ref="1700000000.0001",
                body="see !999 (unknown)",
                idempotency_key="slack-linkify-2",
            )
        sent = bot.post_message.call_args.kwargs["text"]
        assert "!999" in sent
        assert "<http" not in sent

    def _overlay_resolving(self, mr_table: dict[int, str], issue_table: dict[int, str]):
        """Patch the active overlay's token resolvers with synthetic tables."""

        class _Overlay:
            def resolve_mr_token(self, n: int) -> str | None:
                return mr_table.get(n)

            def resolve_issue_token(self, n: int) -> str | None:
                return issue_table.get(n)

        return patch("teatree.core.overlay_loader.get_overlay", return_value=_Overlay())
