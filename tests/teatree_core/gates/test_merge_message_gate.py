"""The published-message gate: a merge publishes only a scanned title/body (§17.4.3 step 7).

Before this gate the merge request sent no commit message, so the forge's template
copied the PR body onto the default branch unscanned, AI footer included. Every
merge route crosses ``execute_bound_merge``; these drive it through the keystone and
the no-CLEAR sweep route with only the ``gh`` subprocess stubbed.
"""

import json
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.backend_protocols import PrMessage
from teatree.core.gates.merge_message_gate import scanned_merge_message
from teatree.core.merge import MergePreconditionError, execute_bound_merge, merge_ticket_pr
from teatree.core.models import MergeClear, Ticket
from teatree.loop.scanners.pr_sweep_adapters import GhPrApiClient
from teatree.utils.pr_ref import PrRef
from tests._forge_stub import CLEAN_PR_BODY, CLEAN_PR_TITLE
from tests._send_gate import TEST_TERM_REGISTRY
from tests.teatree_core.test_merge_execution import _SHA, _clear, _GhStub, _record_merge_safe_verdict

_FOOTER = "\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)"
_REF = PrRef(slug="souliane/teatree", pr_id=859)
_LEAKED_TERM = TEST_TERM_REGISTRY["leak"][0]
_HOOK_ADMITTED_BODY = "Behavior preservation: the selector moved verbatim, so the merge gate's choice is unchanged."
_STRUCTURED_QUOTE = '> "Never merge on a Friday," the owner wrote.'


@pytest.fixture(autouse=True)
def _skip_author_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # The #1773 author gate is exercised by test_merge_execution_author_gate.
    monkeypatch.setattr("teatree.core.merge.execution.assert_merge_provenance_trusted", lambda **_: None)


class _MessageStub(_GhStub):
    """The keystone ``gh`` stub, answering the title/body read with a scripted PR message."""

    def __init__(self, *, title: str = CLEAN_PR_TITLE, body: str = CLEAN_PR_BODY, message_rc: int = 0) -> None:
        super().__init__()
        self.title = title
        self.body = body
        self.message_rc = message_rc

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        if "title,body" in argv:
            self.calls.append(argv)
            return (self.message_rc, json.dumps({"title": self.title, "body": self.body}), "")
        return super().__call__(argv)

    def merge_requests(self) -> list[list[str]]:
        return [argv for argv in self.calls if any(word.endswith("/merge") for word in argv)]


def _keystone(stub: _MessageStub, clear: MergeClear | None = None, *, squash: bool = True) -> None:
    if clear is None:
        clear = _clear(Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED))
    with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=stub):
        merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop", squash=squash)


def _bound_merge(stub: _MessageStub) -> str:
    _record_merge_safe_verdict(pr_id=859, sha=_SHA)
    with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=stub):
        return execute_bound_merge(ref=_REF, expected_head_oid=_SHA)


class TestScannedMergeMessage(TestCase):
    def test_a_robot_footer_refuses_and_names_the_trailer(self) -> None:
        with pytest.raises(MergePreconditionError, match="AI-signature") as refused:
            scanned_merge_message(_REF, PrMessage(title="Tidy the widget", body=f"Tidies it.\n\n{_FOOTER}"))
        assert "generated-with" in str(refused.value)

    def test_a_model_co_author_trailer_refuses(self) -> None:
        body = "Tidies it.\n\nCo-Authored-By: Claude <noreply@anthropic.com>"  # privacy-scan:allow fake trailer fixture
        with pytest.raises(MergePreconditionError, match="AI-signature"):
            scanned_merge_message(_REF, PrMessage(title="Tidy the widget", body=body))

    def test_a_footer_described_in_prose_is_not_a_trailer(self) -> None:
        message = PrMessage(title="Ban the footer", body=f"Refuses a body ending in `{_FOOTER}`.")
        assert scanned_merge_message(_REF, message) == message


@pytest.mark.real_merge_privacy_scan
@pytest.mark.usefixtures("configured_banned_term_registry")
class TestPublicTargetLeakScan(TestCase):
    _BODY = f"Rolls the widget out for {_LEAKED_TERM}."

    def test_a_banned_term_bound_for_a_public_repo_is_refused_before_the_merge_request(self) -> None:
        stub = _MessageStub(body=self._BODY)
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            pytest.raises(MergePreconditionError, match="public-repo leak scan"),
        ):
            _bound_merge(stub)
        assert stub.merge_requests() == []

    def test_the_same_body_to_a_private_repo_merges(self) -> None:
        stub = _MessageStub(body=self._BODY)
        with patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False):
            assert _bound_merge(stub)
        assert stub.merge_requests()

    def test_a_body_the_publish_hook_admitted_merges_to_a_public_repo(self) -> None:
        stub = _MessageStub(body=_HOOK_ADMITTED_BODY)
        with patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True):
            assert _bound_merge(stub)
        assert stub.merge_requests()

    def test_a_structured_user_quote_bound_for_a_public_repo_is_refused(self) -> None:
        stub = _MessageStub(body=_STRUCTURED_QUOTE)
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            pytest.raises(MergePreconditionError, match="blockquote-attributed"),
        ):
            _bound_merge(stub)
        assert stub.merge_requests() == []


class TestKeystonePublishesOnlyTheScannedMessage(TestCase):
    def test_a_footer_body_is_refused_before_any_merge_request(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        clear = _clear(ticket)
        stub = _MessageStub(body=f"Tidies the widget.\n\n{_FOOTER}\n")
        with pytest.raises(MergePreconditionError, match="AI-signature"):
            _keystone(stub, clear)

        ticket.refresh_from_db()
        clear.refresh_from_db()
        assert stub.merge_requests() == []
        assert ticket.state == Ticket.State.REVIEW_REQUESTED
        assert clear.consumed_at is None

    def test_an_unreadable_message_is_refused_before_any_merge_request(self) -> None:
        stub = _MessageStub(message_rc=1)
        with pytest.raises(MergePreconditionError, match="could not be read"):
            _keystone(stub)
        assert stub.merge_requests() == []

    def test_a_clean_merge_sends_exactly_the_scanned_title_and_body(self) -> None:
        stub = _MessageStub(title="Tidy the widget", body="Tidies the widget.\n\nKeeps the gears.")
        _keystone(stub)

        (payload,) = stub.merge_payloads
        assert payload["commit_title"] == "Tidy the widget (#859)"
        assert payload["commit_message"] == "Tidies the widget.\n\nKeeps the gears."

    def test_a_no_squash_merge_also_sends_the_scanned_message(self) -> None:
        stub = _MessageStub(title="Tidy the widget", body="Tidies the widget.")
        _keystone(stub, squash=False)

        (payload,) = stub.merge_payloads
        assert payload["merge_method"] == "merge"
        assert payload["commit_title"] == "Tidy the widget (#859)"
        assert payload["commit_message"] == "Tidies the widget."


class TestSweepRouteIsGated(TestCase):
    def test_the_no_clear_bound_merge_refuses_a_footer_body(self) -> None:
        _record_merge_safe_verdict(pr_id=859, sha=_SHA)
        stub = _MessageStub(body=f"Tidies the widget.\n\n{_FOOTER}")
        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=stub):
            result = GhPrApiClient().merge_pr_bound(slug="souliane/teatree", pr_id=859, expected_head_oid=_SHA)

        assert result.merged is False
        assert "AI-signature" in result.refusal
        assert stub.merge_requests() == []
