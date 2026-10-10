"""A row recorded before cards existed is checked where it is sent, and never reaches the owner unplainly (#4990).

Every owner send goes through ``shown_problems_for``: a card passes by construction, an old row must read
plainly now. One that does not is withheld — moved to the internal queue with an audit row, its root message
(if it was already posted) replaced by a neutral line, and never posted, bumped or counted again.
"""

import datetime as dt
from collections.abc import Callable
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from teatree.core import notify as notify_module
from teatree.core.models import DeferredQuestion
from teatree.core.models.deferred_question import DeferredQuestionAudit
from teatree.core.notify_question_drains import (
    drain_deferred_questions,
    drain_unmirrored_deferred_questions,
    reask_escalated_questions,
    resurface_question_backlog,
)
from teatree.core.owner_question_message import render_blocks, render_text
from tests._owner_channel import legacy_owner_row

_CHANNEL = "D0DEMOOWNER"
_JARGON = "Is the acme_only scope profile what you want for the widget bot?"


def _backend() -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = _CHANNEL
    backend.post_message.return_value = {"ok": True, "ts": "1800000000.000100"}
    backend.update_message.return_value = {"ok": True}
    backend.get_permalink.return_value = "https://acme.slack.example/archives/D0DEMOOWNER/p1800000000000100"
    return backend


def _jargon_row(**fields: object) -> DeferredQuestion:
    return legacy_owner_row(_JARGON, **fields)


def _mirrored_jargon_row() -> DeferredQuestion:
    posted_at = timezone.now() - dt.timedelta(hours=3)
    return _jargon_row(slack_channel=_CHANNEL, slack_ts=f"{posted_at.timestamp():.6f}", created_at=posted_at)


def _run(drain: Callable[..., object], backend: MagicMock) -> None:
    with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
        drain(user_id="U0DEMOOWNER", backend=backend)


class TestAJargonRowIsNeverSent(TestCase):
    def _assert_withheld_and_unsent(self, row: DeferredQuestion, backend: MagicMock) -> None:
        backend.post_message.assert_not_called()
        row.refresh_from_db()
        assert row.audience == DeferredQuestion.Audience.INTERNAL
        assert row.is_pending
        audit = DeferredQuestionAudit.objects.get(question=row, action="withheld")
        assert "acme_only" in audit.note

    def test_the_first_post_drain_withholds_it(self) -> None:
        row, backend = _jargon_row(), _backend()

        _run(drain_unmirrored_deferred_questions, backend)

        self._assert_withheld_and_unsent(row, backend)

    def test_the_resurface_drain_withholds_it(self) -> None:
        row, backend = _jargon_row(), _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            delivered, total = drain_deferred_questions(user_id="U0DEMOOWNER", backend=backend)

        self._assert_withheld_and_unsent(row, backend)
        assert (delivered, total) == (0, 0)

    def test_the_bump_drain_withholds_it_and_counts_nothing(self) -> None:
        row, backend = _mirrored_jargon_row(), _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            bumped, candidates = reask_escalated_questions(user_id="U0DEMOOWNER", backend=backend)

        self._assert_withheld_and_unsent(row, backend)
        assert (bumped, candidates) == (0, 0)

    def test_the_digest_neither_posts_nor_counts_it(self) -> None:
        row, backend = _jargon_row(), _backend()

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            posted, pending = resurface_question_backlog(user_id="U0DEMOOWNER", backend=backend)

        self._assert_withheld_and_unsent(row, backend)
        assert (posted, pending) == (False, 0)

    def test_a_row_without_a_marker_gets_one_that_names_it(self) -> None:
        row = _jargon_row()

        _run(drain_unmirrored_deferred_questions, _backend())

        row.refresh_from_db()
        assert row.dedupe_marker == f"withheld:{row.pk}"

    def test_a_row_with_a_marker_keeps_it_for_the_ask_again(self) -> None:
        row = _jargon_row(dedupe_marker="profile:acme")

        _run(drain_unmirrored_deferred_questions, _backend())

        row.refresh_from_db()
        assert row.dedupe_marker == "profile:acme"

    def test_a_posted_root_is_replaced_by_the_neutral_line_without_buttons(self) -> None:
        row, backend = _mirrored_jargon_row(), _backend()

        _run(drain_deferred_questions, backend)

        backend.update_message.assert_called_once_with(
            channel=_CHANNEL,
            ts=row.slack_ts,
            text="I will ask this again in plain words.",
            blocks=[
                {"type": "context", "elements": [{"type": "mrkdwn", "text": "I will ask this again in plain words."}]}
            ],
        )

    def test_an_unposted_row_has_no_root_to_replace(self) -> None:
        backend = _backend()
        _jargon_row()

        _run(drain_unmirrored_deferred_questions, backend)

        backend.update_message.assert_not_called()

    def test_a_failed_root_update_never_stops_the_drain(self) -> None:
        backend = _backend()
        backend.update_message.side_effect = RuntimeError("slack is down")
        _mirrored_jargon_row()
        clean = legacy_owner_row("Should the acme widget go to production?")

        _run(drain_unmirrored_deferred_questions, backend)

        clean.refresh_from_db()
        assert clean.slack_ts

    def test_a_row_recorded_with_no_evidence_at_all_is_checked_too(self) -> None:
        row = DeferredQuestion.objects.create(question=_JARGON, audience=DeferredQuestion.Audience.OWNER_QUESTION)
        backend = _backend()

        _run(drain_unmirrored_deferred_questions, backend)

        backend.post_message.assert_not_called()
        row.refresh_from_db()
        assert row.audience == DeferredQuestion.Audience.INTERNAL

    def test_a_row_that_says_deferred_in_any_case_is_withheld(self) -> None:
        row, backend = legacy_owner_row("Was the widget DEFERRED while you were away?"), _backend()

        _run(drain_unmirrored_deferred_questions, backend)

        backend.post_message.assert_not_called()
        row.refresh_from_db()
        assert row.audience == DeferredQuestion.Audience.INTERNAL


class TestACleanLegacyRowIsSentThroughTheSameRenderer(TestCase):
    def test_a_clean_row_is_posted_with_one_button_per_option_and_the_ref_line(self) -> None:
        row = legacy_owner_row(
            "Should the acme widget go to production?", options_json='[{"label": "Yes"}, {"label": "No"}]'
        )
        backend = _backend()

        _run(drain_unmirrored_deferred_questions, backend)

        backend.post_message.assert_called_once()
        kwargs = backend.post_message.call_args.kwargs
        assert kwargs["text"] == render_text(row)
        assert kwargs["blocks"] == render_blocks(row)
        buttons = [e for block in kwargs["blocks"] if block["type"] == "actions" for e in block["elements"]]
        assert [button["text"]["text"] for button in buttons] == ["Yes", "No"]
        assert kwargs["text"].endswith(f"ref: question {row.pk}")
        for old in ("Pending question", "deferred", "typed reply", ":question:"):
            assert old not in kwargs["text"]

    def test_a_clean_row_is_never_withheld(self) -> None:
        row = legacy_owner_row("Should the acme widget go to production?")

        _run(drain_unmirrored_deferred_questions, _backend())

        row.refresh_from_db()
        assert row.audience == DeferredQuestion.Audience.OWNER_QUESTION
        assert not DeferredQuestionAudit.objects.filter(action="withheld").exists()
