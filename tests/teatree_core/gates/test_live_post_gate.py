"""The #1207 live-post refusal must name the paths that need NO approval token.

The gate itself is unchanged and stays hard: a colleague-visible ``post-comment
--live`` still requires a Slack-recorded :class:`LivePostApproval`, on the
owner's own MR as much as anywhere else — ``/t3:rules`` § "Ask Before Posting on
the User's Behalf" enumerates ``post_comment --live`` among the surfaces the
author-side carve-out deliberately does NOT cover.

What was missing is that the refusal named the approval token as if it were the
only way forward, while two ungated paths already existed: the DRAFT default, and
``t3 review reply-to-discussion`` (the proved-authorship author-side carve-out,
which never calls this gate). An agent whose comment was a review finding on the
owner's own MR read the refusal and stalled. Naming them relaxes nothing.

The classes below split the two concerns MR !225's review flagged as conflated:
the STRING checks (``TestRefusalStillRefuses`` / ``TestRefusalNamesTheUngatedPaths``)
prove the wording only; :class:`TestGateBehaviourNotJustWording` proves the
BEHAVIOUR the wording exists to describe — through the REAL gate chokepoint
(:func:`publish_live_post`, :meth:`LivePostApproval.record`), never a stub — so a
future change that breaks the guidance-behaviour link (e.g. a refusal that still
reads right while the underlying gate silently stopped blocking) turns this file
red.
"""

import os
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.cli.review.default_draft import publish_live_post
from teatree.core.gates.live_post_gate import APPROVE_LIVE_POST_USAGE, UNGATED_ALTERNATIVES, LivePostBlockedError
from teatree.core.modelkit.question_card import QuestionCard, plain_text
from teatree.core.models import DeferredQuestion, LivePostApproval
from teatree.core.models.live_post_approval import canonical_mr_scope

_MR = "acme-internal/widget!42"


@pytest.fixture
def refusal() -> str:
    return str(LivePostBlockedError(_MR))


class TestRefusalStillRefuses:
    def test_it_is_an_error_naming_the_mr(self, refusal: str) -> None:
        assert "live post blocked (#1207)" in refusal
        assert _MR in refusal

    def test_the_recorded_approval_remains_the_way_to_post_live(self, refusal: str) -> None:
        assert APPROVE_LIVE_POST_USAGE in refusal
        assert "approve-live-post" in refusal


class TestRefusalNamesTheUngatedPaths:
    def test_it_carries_the_alternatives(self, refusal: str) -> None:
        assert UNGATED_ALTERNATIVES in refusal

    def test_it_names_the_draft_default(self, refusal: str) -> None:
        assert "DRAFT" in refusal
        assert "colleague-invisible" in refusal

    def test_it_names_the_author_side_reply_on_the_owners_own_mr(self, refusal: str) -> None:
        assert "reply-to-discussion" in refusal
        assert "OWNER AUTHORED" in refusal

    def test_it_does_not_present_a_bypass(self, refusal: str) -> None:
        # `/t3:rules` § "Anticipate a Predictable Gate": the options offered are
        # enable-durably or approve-once, never "turn the gate off".
        lowered = refusal.lower()
        assert "--no-verify" not in lowered
        assert "disable" not in lowered
        assert "bypass" not in lowered


class TestGateBehaviourNotJustWording(TestCase):
    """MINOR (MR !225 finding): the wording's behavioural claims, proved for real.

    The refusal genuinely BLOCKS an unauthorised live post, and the recorded
    approval it names genuinely CLEARS it — through the real
    :func:`publish_live_post` chokepoint, not a mocked/stubbed refusal.
    """

    def test_no_recorded_approval_refuses_and_the_publish_callable_is_never_invoked(self) -> None:
        # "Must NOT publish" (docstring) is a behavioural claim, not a wording one:
        # the publish callable must never even run.
        calls: list[str] = []

        def publish() -> tuple[str, int]:
            calls.append("published")
            return "OK", 0

        message, code = publish_live_post(repo="acme-internal/widget", mr=42, publish=publish)

        assert code == 1
        assert "live post blocked (#1207)" in message
        assert calls == [], "unauthorised: the publish body must never run"

    def test_refused_live_post_asks_once_and_approval_settles_the_question(self) -> None:
        for _ in range(2):
            message, code = publish_live_post(repo="acme-internal/widget", mr=42, publish=lambda: ("OK", 0))
            assert code == 1
            assert "live post blocked" in message

        rows = list(DeferredQuestion.owner_pending())
        assert len(rows) == 1
        assert rows[0].evidence["decision"] == "public_post"
        assert canonical_mr_scope(rows[0].evidence["subject"]) == _MR

        LivePostApproval.record(mr_url=_MR, slack_ts="1700000000.000100", slack_user_id="U-OWNER")
        assert publish_live_post(repo="acme-internal/widget", mr=42, publish=lambda: ("OK", 0)) == ("OK", 0)
        rows[0].refresh_from_db()
        assert rows[0].status == DeferredQuestion.STATUS_DISMISSED

        message, code = publish_live_post(repo="acme-internal/widget", mr=42, publish=lambda: ("OK", 0))
        assert code == 1
        assert "live post blocked" in message
        pending = list(DeferredQuestion.owner_pending())
        assert len(pending) == 1
        assert pending[0].pk != rows[0].pk

    def test_refusal_card_links_the_actual_review_host(self) -> None:
        with patch.dict(os.environ, {"GITLAB_URL": "https://gitlab.example.com/api/v4"}):
            _message, code = publish_live_post(repo="acme-internal/widget", mr=42, publish=lambda: ("OK", 0))

        assert code == 1
        row = DeferredQuestion.owner_pending().get()
        card = QuestionCard.from_row(row.evidence, row.options_json)
        assert card is not None
        assert card.subject == "https://gitlab.example.com/acme-internal/widget/-/merge_requests/42"
        assert card.subject in plain_text(card.view(row.question))

    def test_unreadable_review_url_still_blocks_and_records_the_wait(self) -> None:
        with (
            patch(
                "teatree.cli.review.forge_target.resolve_base_url", side_effect=RuntimeError("forge URL unavailable")
            ),
            self.assertLogs("teatree.core.gates.live_post_gate", level="ERROR") as logs,
        ):
            _message, code = publish_live_post(repo="acme-internal/widget", mr=42, publish=lambda: ("OK", 0))

        assert code == 1
        assert DeferredQuestion.owner_pending().count() == 1
        assert any("forge URL unavailable" in line for line in logs.output)

    def test_a_genuine_recorded_approval_clears_the_gate_and_is_consumed_single_use(self) -> None:
        LivePostApproval.record(mr_url="acme-internal/widget!42", slack_ts="1700000000.000100", slack_user_id="U-OWNER")

        message, code = publish_live_post(repo="acme-internal/widget", mr=42, publish=lambda: ("OK note_id=1", 0))

        assert (message, code) == ("OK note_id=1", 0)
        assert LivePostApproval.objects.filter(mr_url="acme-internal/widget!42", consumed_at__isnull=False).count() == 1

    def test_an_approval_for_a_different_mr_does_not_authorise_this_one(self) -> None:
        # Negative assertion against unauthorised publishing: an approval is
        # strictly MR-scoped, so a token recorded for !43 must not clear !42 — the
        # same shape of failure as a copied/misattributed authorisation token
        # (MR !225's MAJOR finding) reused outside the scope it was granted for.
        LivePostApproval.record(mr_url="acme-internal/widget!43", slack_ts="1700000000.000100", slack_user_id="U-OWNER")
        calls: list[str] = []

        def publish() -> tuple[str, int]:
            calls.append("published")
            return "OK", 0

        message, code = publish_live_post(repo="acme-internal/widget", mr=42, publish=publish)

        assert code == 1
        assert "live post blocked (#1207)" in message
        assert calls == []
