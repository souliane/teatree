"""Requester-bound replies and registered-operator new work in Slack."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.core.models import OperatorIdentity, ThreadBinding
from teatree.loop.inbound_reading import InboundIntent, InboundReader, memoized_reader
from teatree.loop.question_binding import BoundAnswer, bind_reply, configured_owner_id, log_coalesced_replies
from teatree.loop.slack_answer.control_verbs import control_verb
from teatree.loop.slack_answer.priority_directive import parse_priority_directive
from teatree.loop.slack_answer.session_routing import _reject_foreign_unit

if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend
    from teatree.loop.slack_answer.coalescing import _Unit
    from teatree.loop.slack_answer.cycle import SlackAnswerReport


@dataclass(frozen=True, slots=True)
class ForeignHandlers:
    binding: ThreadBinding | None
    react_eyes_once: Callable[["MessagingBackend", "_Unit"], bool]
    route_unbound: Callable[
        ["MessagingBackend", "_Unit", "SlackAnswerReport", InboundReader, ThreadBinding | None], None
    ]
    answer_question: Callable[["MessagingBackend", "_Unit", "SlackAnswerReport", BoundAnswer], bool]


def handle_foreign_question(
    backend: "MessagingBackend",
    unit: "_Unit",
    report: "SlackAnswerReport",
    reader: InboundReader,
    handlers: ForeignHandlers,
) -> bool:
    owner_id = configured_owner_id(backend)
    if owner_id and unit.lead.user_id == owner_id:
        return False
    result = bind_reply(unit.lead, reader=memoized_reader(reader), backend=backend, text=unit.text)
    if not isinstance(result, BoundAnswer):
        operator = OperatorIdentity.objects.filter(
            platform="slack", handle=unit.lead.user_id, user__is_active=True, user__is_superuser=True
        ).exists()
        if (
            operator
            and handlers.binding is None
            and control_verb(unit.text) is None
            and parse_priority_directive(unit.text) is None
        ):
            reading = reader(unit.text)
            if reading.work_summary and reading.intent not in {InboundIntent.FYI, InboundIntent.NOISE}:
                if handlers.react_eyes_once(backend, unit):
                    report.eyes_reacted += 1
                handlers.route_unbound(backend, unit, report, memoized_reader(reader), handlers.binding)
                return True
        _reject_foreign_unit(backend, unit, report)
        return True
    if handlers.react_eyes_once(backend, unit):
        report.eyes_reacted += 1
    log_coalesced_replies(unit.rows, result)
    handlers.answer_question(backend, unit, report, result)
    return True
