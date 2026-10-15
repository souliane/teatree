"""Ask the owner to approve a merge correction on a durable card."""

from teatree.core.merge.void_merge import VoidPlan, VoidRefusal, VoidRefusedError, plan_void
from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind, action_marker
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard, safe_card_title
from teatree.core.models import Ticket
from teatree.core.models.deferred_question import DeferredQuestion


def _subject(plan: VoidPlan) -> str:
    ticket_ids = [item["ticket_id"] for item in plan.restores]
    ticket = Ticket.objects.filter(pk__in=ticket_ids).order_by("pk").first()
    title = (ticket.short_description or ticket.extra.get("issue_title", "")) if ticket is not None else ""
    title = safe_card_title(str(title), "this pull request")
    return f"<{plan.pr_url}|{title}>"


def request_void(pr_url: str, reason: str) -> DeferredQuestion:
    if not reason.strip():
        raise VoidRefusedError(VoidRefusal.REASON_MISSING, "the reason for voiding the merge is missing")
    plan = plan_void(pr_url)
    if plan.refusal is not None:
        raise plan.refusal
    changed = len(plan.verdict.changed)
    restores = len(plan.restores)
    closes = len(plan.closes)
    action = ActionRequest(kind=OwnerActionKind.VOID, target=plan.pr_url, binding=plan.binding(), note=reason.strip())
    question = f"Should I void the recorded merge of {_subject(plan)}?"
    card = QuestionCard(
        decision=OwnerDecision.IRREVERSIBLE,
        checked=(
            "The forge records this pull request as merged.",
            f"None of its {changed} changed files are on the default branch.",
            f"This returns {restores} tickets to their state before the merge.",
            f"This closes {closes} pull request records.",
        ),
        blocker="Only the owner may rewrite a recorded merge.",
        why=f"The forge reports a merge, but none of its {changed} changed files reached the default branch.",
        options=(
            CardOption(
                "Void",
                f"I close {closes} merged records and return {restores} tickets to their earlier state.",
                recommended=True,
            ),
            CardOption("Keep", "Nothing changes."),
        ),
        action=action,
    ).with_quote(question, reason)
    return DeferredQuestion.record(question, dedupe_marker=action_marker(action), card=card)
