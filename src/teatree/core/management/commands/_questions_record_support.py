"""What ``questions record`` needs besides the row it writes: its options and the hand-off to the coordinator."""

from asgiref.sync import async_to_sync
from django.core.management.base import OutputWrapper

from teatree.agents.coordinator_escalation import escalate
from teatree.agents.live_mailbox_client import MailboxClient, MailboxError
from teatree.core.answering.escalation import EscalationRefusedError
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption

_REFUSED = 2


def parsed_options(options_json: str, stderr: OutputWrapper) -> tuple[CardOption, ...]:
    options = CardOption.parse_all(options_json)
    if options_json and not options:
        stderr.write("--options must be a JSON list of {label, description, recommended}")
        raise SystemExit(_REFUSED)
    return options


def hand_to_coordinator(need: str, stderr: OutputWrapper) -> str:
    client = MailboxClient.from_env()
    if client is None:
        stderr.write("a need for the coordinator travels over a running task's mailbox; this process has none")
        raise SystemExit(_REFUSED)
    try:
        receipt = async_to_sync(escalate)(client, kind=OwnerDecision.PRODUCT_SCOPE.value, message=need)
    except (EscalationRefusedError, MailboxError) as exc:
        stderr.write(str(exc))
        raise SystemExit(_REFUSED) from exc
    return (
        f"sent to the coordinator as request #{receipt['request_id']} ({receipt['delivery']}); "
        f"no owner card was raised. The answer comes to your mailbox; "
        f"unanswered after {receipt['deadline_minutes']} minutes, this task fails."
    )
