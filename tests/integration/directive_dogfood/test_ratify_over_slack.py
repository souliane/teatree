"""The proof-case directive is ratified by a Slack reply from the owner, bound through the real reply scanner."""

from django.core.management import call_command
from django.test import TestCase

from teatree.core.gates.directive_interpret_gate import record_returned_directive_interpretation
from teatree.core.models import DeferredQuestion, DirectiveDispatch, DmContext, PendingChatInjection
from teatree.core.models.directive import Directive
from teatree.loop.scanners.askuserquestion_reply import AskUserQuestionReplyScanner
from tests._owner_channel import OWNER_SLACK_ID
from tests.integration.directive_dogfood.exemplar import EXEMPLAR_ENVELOPE, PROOF_CASE_TEXT, SCOPE, tick
from tests.teatree_loop.test_question_binding import FakeMessaging, _statement


def _ratify_asked() -> Directive:
    call_command("directive", "capture", PROOF_CASE_TEXT, scope=SCOPE)
    directive = Directive.objects.get()
    tick()
    record_returned_directive_interpretation(DirectiveDispatch.objects.get(directive=directive).task, EXEMPLAR_ENVELOPE)
    tick()
    directive.refresh_from_db()
    assert directive.state == Directive.State.RATIFY_PENDING
    return directive


def _reply_on_slack(question: DeferredQuestion, *, author: str) -> None:
    assert PendingChatInjection.record(
        channel="D-owner", slack_ts="900.0", text=f"#{question.pk} approve", context=DmContext(user_id=author)
    )
    AskUserQuestionReplyScanner(backend=FakeMessaging(), overlay="", reader=_statement).scan()


class TestRatifyOverSlack(TestCase):
    def test_an_owner_reply_on_slack_ratifies_the_directive(self) -> None:
        directive = _ratify_asked()
        question = directive.ratify_question
        _reply_on_slack(question, author=OWNER_SLACK_ID)
        question.refresh_from_db()
        assert question.resolved_via == DeferredQuestion.ResolvedVia.SLACK

        tick()

        directive.refresh_from_db()
        assert directive.state == Directive.State.ADMITTED

    def test_a_non_owner_reply_on_slack_does_not_ratify_the_directive(self) -> None:
        directive = _ratify_asked()
        question = directive.ratify_question
        _reply_on_slack(question, author="U2")
        question.refresh_from_db()
        assert question.is_pending

        tick()

        directive.refresh_from_db()
        assert directive.state == Directive.State.RATIFY_PENDING
        assert directive.ratify_question.pk == question.pk
