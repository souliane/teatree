"""A card, a bump and the digest are sent as written: the link rewriter never touches an owner question (#4990)."""

import datetime as dt
from contextlib import AbstractContextManager
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from teatree.core import notify as notify_module
from teatree.core.models import DeferredQuestion
from teatree.core.notify_question_drains import (
    drain_unmirrored_deferred_questions,
    reask_escalated_questions,
    resurface_question_backlog,
)
from teatree.core.owner_question_message import render_text
from tests._owner_channel import OWNER_DECISION

_REWRITTEN = "REWRITTEN"
_OWNER = "U0DEMOOWNER"


def _backend() -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = "D0DEMOOWNER"
    backend.post_message.return_value = {"ok": True, "ts": "1800000000.000100"}
    backend.get_permalink.return_value = "https://acme.slack.example/archives/D0DEMOOWNER/p1800000000000100"
    return backend


def _rewriter() -> AbstractContextManager[object]:
    return patch.object(notify_module, "maybe_linkify", side_effect=lambda text: f"{_REWRITTEN} {text}")


def _posted(backend: MagicMock) -> str:
    return str(backend.post_message.call_args.kwargs["text"])


class TestNoSendIsRewritten(TestCase):
    def _three_hour_old_mirrored_row(self) -> DeferredQuestion:
        posted_at = timezone.now() - dt.timedelta(hours=3)
        row = DeferredQuestion.record(
            "Can I ship the widgets?",
            slack_channel="D0DEMOOWNER",
            slack_ts=f"{posted_at.timestamp():.6f}",
            **OWNER_DECISION,
        )
        DeferredQuestion.objects.filter(pk=row.pk).update(created_at=posted_at)
        return row

    def test_the_first_post_is_the_rendered_card(self) -> None:
        row, backend = DeferredQuestion.record("Can I ship the widgets?", **OWNER_DECISION), _backend()

        with _rewriter(), patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            drain_unmirrored_deferred_questions(user_id=_OWNER, backend=backend)

        assert _posted(backend) == render_text(row)

    def test_the_bump_is_not_rewritten(self) -> None:
        self._three_hour_old_mirrored_row()
        backend = _backend()

        with _rewriter(), patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            reask_escalated_questions(user_id=_OWNER, backend=backend)

        assert _posted(backend).startswith("Still waiting for your decision")

    def test_the_digest_is_not_rewritten(self) -> None:
        self._three_hour_old_mirrored_row()
        backend = _backend()

        with _rewriter(), patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            resurface_question_backlog(user_id=_OWNER, backend=backend)

        assert _REWRITTEN not in _posted(backend)
