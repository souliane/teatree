import re
from dataclasses import asdict
from pathlib import Path

from teatree.core.modelkit.escape_scope import EscapeScope
from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind, action_marker
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard, safe_card_title
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.hooks.gate_escape import EscapePolicy, policy_for

_GATE_LABELS = {
    "out_of_band_merge": "a merge outside the normal route",
    "raw_review_post": "a review post outside the normal route",
    "raw_issue_write": "an issue write outside the normal route",
}
_PLAIN_WORD = re.compile(r"[A-Za-z][A-Za-z-]*")
_PLAIN_COMMAND_WORDS = 3


def _command_in_plain_words(excerpt: str) -> str:
    words: list[str] = []
    for token in excerpt.split()[:_PLAIN_COMMAND_WORDS]:
        if not _PLAIN_WORD.fullmatch(token):
            break
        words.append(token)
    return " ".join(words) or "a command"


def request_owner_escape(scope: EscapeScope) -> DeferredQuestion:
    if policy_for(scope.gate) is not EscapePolicy.HUMAN_ONLY:
        message = "Only a human-only gate can request owner approval"
        raise ValueError(message)
    action = ActionRequest(
        kind=OwnerActionKind.ESCAPE,
        target=scope.gate,
        binding=scope.binding(),
        scope=asdict(scope),
    )
    question = f"Should I allow one exception for {_GATE_LABELS[scope.gate]}?"
    card = QuestionCard(
        decision=OwnerDecision.CREDENTIALS,
        checked=(
            "The gate blocked one specific action.",
            "A matching approval expires after one hour.",
            "The action is still blocked until the audit is written.",
        ),
        blocker="Only the owner may approve this exception.",
        why=(
            f"The agent was blocked in {safe_card_title(Path(scope.subject).name, 'the current project')} "
            f"while running {_command_in_plain_words(scope.excerpt)}."
        ),
        quoted=safe_card_title(scope.reason, "The agent reported a false block."),
        options=(
            CardOption("Approve", "Allow the one matching action once.", recommended=True),
            CardOption("Decline", "Keep the action blocked."),
        ),
        action=action,
    )
    row = DeferredQuestion.record(
        question,
        dedupe_marker=action_marker(action),
        session_id=scope.session_id,
        tool_use_id=scope.call_id,
        card=card,
    )
    if not row.is_pending:
        message = "The owner question could not be queued"
        raise ValueError(message)
    return row
