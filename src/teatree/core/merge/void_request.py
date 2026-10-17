"""Ask the owner to approve a merge correction on a durable card."""

from teatree.core.merge.void_merge import VoidPlan, VoidRefusal, VoidRefusedError, plan_void
from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind, action_marker
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard, counted, safe_ticket_card_title
from teatree.core.models import Ticket
from teatree.core.models.deferred_question import DeferredQuestion


def _subject(plan: VoidPlan) -> str:
    ticket_ids = [item["ticket_id"] for item in plan.restores]
    ticket = Ticket.objects.filter(pk__in=ticket_ids).order_by("pk").first()
    title = (
        safe_ticket_card_title(ticket.short_description, ticket.extra, "this pull request")
        if ticket is not None
        else "this pull request"
    )
    return f"<{plan.pr_url}|{title}>"


def request_void(pr_url: str, reason: str) -> DeferredQuestion:
    if not reason.strip():
        raise VoidRefusedError(VoidRefusal.REASON_MISSING, "the reason for voiding the merge is missing")
    plan = plan_void(pr_url)
    if plan.refusal is not None:
        raise plan.refusal
    changed = counted(len(plan.verdict.changed), "changed file")
    restores = counted(len(plan.restores), "ticket")
    closes = counted(len(plan.closes), "pull request record")
    action = ActionRequest(kind=OwnerActionKind.VOID, target=plan.pr_url, binding=plan.binding(), note=reason.strip())
    question = f"Should I void the recorded merge of {_subject(plan)}?"
    card = QuestionCard(
        decision=OwnerDecision.IRREVERSIBLE,
        checked=(
            "The forge records this pull request as merged.",
            f"The default branch holds none of its {changed}.",
            f"It returns {restores} to the state before the merge.",
            f"It closes {closes}.",
        ),
        blocker="Only the owner may rewrite a recorded merge.",
        why=f"The forge reports a merge, but the default branch holds none of its {changed}.",
        options=(
            CardOption(
                "Void", f"I close {closes} and return {restores} to the state before the merge.", recommended=True
            ),
            CardOption("Keep", "Nothing changes."),
        ),
        action=action,
    ).with_quote(question, reason)
    row = DeferredQuestion.record(question, dedupe_marker=action_marker(action), card=card)
    if not row.is_pending:
        message = "the owner question could not be queued; ask again"
        raise VoidRefusedError(VoidRefusal.OWNER_QUESTION_UNAVAILABLE, message)
    return row
