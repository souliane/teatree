"""Max-tokens truncation detection + owner escalation for the headless runner.

A ``pydantic_ai`` run cut off at the ``max_tokens`` ceiling amputates its result
envelope and is recorded FAILED by the driver. Silent truncation is the defect
this closes, so the owner is ALSO told through the audited owner egress — the
ceiling can then be raised deliberately. Split out of :mod:`teatree.agents.headless`
as its own concern so the driver stays focused on the run loop.
"""

import logging

from claude_agent_sdk import ResultMessage

from teatree.agents.pydantic_ai_session import MAX_TOKENS_TRUNCATION_SUBTYPE
from teatree.config import get_effective_settings
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import Task
from teatree.core.notify import NotifyKind, notify_user

logger = logging.getLogger(__name__)


def is_max_tokens_truncation(message: ResultMessage | None) -> bool:
    """Whether a failed run is a ``pydantic_ai`` max-tokens truncation.

    Keys on the one terminal subtype the pydantic_ai session stamps when the final
    ``ModelResponse`` stopped on the token ceiling
    (:data:`~teatree.agents.pydantic_ai_session.MAX_TOKENS_TRUNCATION_SUBTYPE`). No other
    backend produces it, so the truncation alert is naturally scoped to the pydantic_ai lane
    with no ``isinstance`` on the harness.
    """
    return message is not None and message.subtype == MAX_TOKENS_TRUNCATION_SUBTYPE


def alert_owner_max_tokens_truncation(task: Task, *, phase: str) -> None:
    """Escalate a max-tokens truncation to the owner — loud, never silent.

    A run cut off at the ``max_tokens`` ceiling amputates the result envelope and is
    recorded FAILED by the caller; silent truncation is exactly the defect this closes, so
    the owner must ALSO be told, ERROR-logged and DM'd through the audited owner egress
    (:func:`teatree.core.notify.notify_user`, ``OWNER_ESCALATION``), so the ceiling can be
    raised deliberately. Names the work item, the phase, and the ceiling — never the
    truncated content. Best-effort: the egress never raises, and a failure here must never
    mask the recorded failure the caller returns.
    """
    ceiling = get_effective_settings().pydantic_ai_max_tokens
    subject = task.display_subject()
    named_phase = phase or task.phase
    logger.error(
        "Task %s (%s) truncated at the %s-token max_tokens ceiling in phase %s — result envelope incomplete",
        task.pk,
        subject,
        ceiling,
        named_phase,
    )
    text = (
        f"Output truncated at the {ceiling}-token `max_tokens` ceiling on {subject} "
        f"(phase `{named_phase}`). The pydantic_ai result envelope was cut off mid-generation "
        "(finish_reason='length') and the run was recorded FAILED — raise `pydantic_ai_max_tokens` "
        "if this recurs."
    )
    try:
        notify_user(
            text,
            kind=NotifyKind.INFO,
            idempotency_key=f"max-tokens-truncation:{task.pk}:{named_phase}",
            audience=NotifyAudience.OWNER_ESCALATION,
        )
    except Exception:
        logger.debug("max-tokens truncation owner alert failed for task %s", task.pk, exc_info=True)
