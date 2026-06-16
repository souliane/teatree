"""Signal-driven on-behalf reactions fire the #949 after-receipt DM.

The ✅ approval reaction and the per-transition emoji reaction are posts
made under the user's identity on a colleague-facing Slack message, so a
*successful* reaction (the helper actually reacted, count > 0) must be
followed by exactly one ``on_behalf_post:`` bot→user DM.

Scope guarantee: a keystone merge transition that reacts nothing (no PR
permalink → ``add_reactions_for_transition`` returns 0) must NOT emit an
after-receipt DM — the merge FSM transition itself is internal
orchestration, never a colleague-visible post.

The Slack reaction boundary is patched (``add_approval_reaction`` /
``add_reactions_for_transition``); the ``notify_user`` orchestration and
the BotPing ledger run for real. The on-behalf pre-gate is set to
``immediate`` via the test config so these tests isolate the
*after*-receipt behaviour.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

import teatree.core.signals as signals_mod
from teatree.core.models import BotPing, PullRequest, Ticket


class _FakeReactionPublisher:
    def __init__(
        self,
        *,
        transition: Callable[..., int] | None = None,
        approval: Callable[..., int] | None = None,
    ):
        self._transition = transition or (lambda *_a, **_k: 0)
        self._approval = approval or (lambda *_a, **_k: 0)

    def add_reactions_for_transition(self, ticket: object, transition_name: str) -> int:
        return self._transition(ticket, transition_name)

    def add_approval_reaction(self, pull_request: object) -> int:
        return self._approval(pull_request)


def _patch_transition_publisher(fn: Callable[..., int]) -> AbstractContextManager[object]:
    return patch.object(signals_mod, "get_reaction_publisher", lambda: _FakeReactionPublisher(transition=fn))


def _patch_approval_publisher(fn: Callable[..., int]) -> AbstractContextManager[object]:
    return patch.object(signals_mod, "get_reaction_publisher", lambda: _FakeReactionPublisher(approval=fn))


def _notify_backend() -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = "D-OPERATOR"
    backend.post_message.return_value = {"ok": True, "ts": "1700000000.0001"}
    backend.get_permalink.return_value = "https://slack.example/archives/D-OPERATOR/p1"
    return backend


class TestSignalsAfterReceiptDm(TestCase):
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # ``slack_user_id`` is a RAW key (TOML-home) — notify_user resolves the
        # user id from it. ``on_behalf_post_mode`` is DB-home (#1775) so a TOML
        # value for it is ignored on read; stage the immediate (gate-off) mode
        # via the ``T3_*`` env tier instead.
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text(
            '[teatree]\nslack_user_id = "U-OPERATOR"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
        monkeypatch.setenv("T3_ON_BEHALF_POST_MODE", "immediate")
        monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", _notify_backend)
        self.monkeypatch = monkeypatch

    def _pr(self) -> PullRequest:
        ticket = Ticket.objects.create(overlay="test")
        pr = PullRequest.objects.create(
            ticket=ticket,
            overlay="test",
            url="https://gitlab.com/org/repo/-/merge_requests/7",
            repo="org/repo",
            iid="7",
            state=PullRequest.State.OPEN,
        )
        pr.request_review(slack_url="https://team.slack.com/archives/C9/p1700000000000100")
        pr.save()
        return pr

    def test_approval_reaction_emits_after_receipt_dm(self) -> None:
        pr = self._pr()
        with _patch_approval_publisher(lambda _p: 1):
            pr.approve()
            pr.save()

        ping = BotPing.objects.get(idempotency_key=f"on_behalf_post:{pr.url}:approval_reaction")
        assert ping.status == BotPing.Status.SENT
        assert "approval reaction" in ping.text

    def test_approval_reaction_no_dm_when_nothing_reacted(self) -> None:
        """No DM when the helper reacted nothing (no review message to react on)."""
        pr = self._pr()
        with _patch_approval_publisher(lambda _p: 0):
            pr.approve()
            pr.save()

        assert not BotPing.objects.filter(idempotency_key__startswith="on_behalf_post:").exists()

    def test_keystone_merge_does_not_emit_after_receipt_dm(self) -> None:
        """A merge transition reacting nothing must NOT fire an after-receipt DM.

        The keystone §17.4 merge transitions the Ticket to MERGED. That
        FSM transition is internal orchestration — it is NOT itself a
        colleague-visible on-behalf post, so with no review message to
        react on (helper returns 0) no ``on_behalf_post:`` DM is sent.
        """
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IN_REVIEW, extra={"mrs": {}})
        with _patch_transition_publisher(lambda _t, _n: 0):
            ticket.mark_merged()
            ticket.save()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert not BotPing.objects.filter(idempotency_key__startswith="on_behalf_post:").exists()

    def test_transition_reaction_emits_after_receipt_dm_when_reacted(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test",
            state=Ticket.State.IN_REVIEW,
            extra={"mrs": {"https://x/1": {"review_permalink": "https://t.slack.com/archives/C1/p1700000000000100"}}},
        )
        with _patch_transition_publisher(lambda _t, _n: 1):
            ticket.mark_merged()
            ticket.save()

        ping = BotPing.objects.get(idempotency_key=f"on_behalf_post:ticket:{ticket.pk}:transition_reaction:mark_merged")
        assert ping.status == BotPing.Status.SENT
