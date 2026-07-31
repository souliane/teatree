"""Ceiling-truncation detection + owner escalation for the headless runner.

Two ceilings can cut a run short, and BOTH must fail loud rather than quietly
hand back less than the work: the ``pydantic_ai`` per-request ``max_tokens``
ceiling, which amputates the result envelope mid-generation, and the per-run
``headless_max_turns`` ceiling, which ends the run between turns. Each is recorded
FAILED by the driver; silent truncation is the defect this closes, so the owner is
ALSO told through the audited owner egress and the ceiling can then be raised
deliberately. Split out of :mod:`teatree.agents.headless` as its own concern so the
driver stays focused on the run loop.
"""

import logging

from claude_agent_sdk import ResultMessage

from teatree.agents.pydantic_ai_session import MAX_TOKENS_TRUNCATION_SUBTYPE
from teatree.config import get_effective_settings
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import Task
from teatree.core.notify import NotifyKind, notify_user

logger = logging.getLogger(__name__)

#: The terminal ``ResultMessage`` subtype a run carries when it ended because it
#: reached its own per-run turn ceiling — emitted by the ``claude`` CLI for the
#: ``ClaudeAgentOptions.max_turns`` cap (``headless_max_turns``) and stamped by
#: :mod:`teatree.agents.pydantic_ai_session` for that lane's ``UsageLimits`` request
#: cap (``pydantic_ai_request_limit``). ONE subtype for one meaning, so both lanes
#: land in the same branch. It distinguishes "the run was cut off at its ceiling"
#: from every other failed result, so a cap is never mistaken for an ordinary error
#: — nor an ordinary error laundered into a cap.
TURN_CEILING_SUBTYPE = "error_max_turns"


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


def is_max_turns_truncation(message: ResultMessage | None) -> bool:
    """Whether a failed run was cut off at the per-run turn ceiling.

    Keys on the single terminal subtype the ``claude`` CLI stamps when it stops a
    run at ``--max-turns`` (:data:`TURN_CEILING_SUBTYPE`), so a cap is recognised by
    the CLI's own structured signal rather than inferred from a turn count — a
    ``num_turns`` that merely equals the ceiling is not proof the ceiling ended the
    run, and a run the ceiling DID end is unambiguous here.
    """
    return message is not None and message.subtype == TURN_CEILING_SUBTYPE


def max_turns_failure_reason(message: ResultMessage | None) -> str:
    """The recorded-failure reason for a run cut off at its own turn ceiling.

    Names the subtype, the turns actually taken, and BOTH lanes' ceiling settings, so
    the truncation is diagnosable from the stored ``TaskAttempt.error`` alone — without
    it a capped run is indistinguishable from any other failed result. Both settings are
    named rather than resolved: :data:`TURN_CEILING_SUBTYPE` is emitted by either lane,
    and guessing which one produced a given result from ambient config would be an
    inference the message does not need to make.
    """
    turns = message.num_turns if message is not None else 0
    return (
        f"turn ceiling reached (subtype={TURN_CEILING_SUBTYPE}): the run was stopped at "
        f"{turns} turns before it produced a result envelope — raise the per-run turn "
        "ceiling if this phase genuinely needs more (`headless_max_turns` on the "
        "claude_sdk lane, `pydantic_ai_request_limit` on the pydantic_ai lane)"
    )


def alert_owner_max_turns_truncation(task: Task, *, phase: str, message: ResultMessage | None = None) -> None:
    """Escalate a turn-ceiling truncation to the owner — loud, never silent.

    A run stopped at the turn ceiling ends between turns with no result envelope and
    is recorded FAILED by the caller. A cap that silently converts a runaway into a
    truncation is the failure mode the cap must not introduce, so the owner is ALSO
    ERROR-logged and DM'd through the audited owner egress
    (:func:`teatree.core.notify.notify_user`, ``OWNER_ESCALATION``) and can raise the
    ceiling deliberately. Names the work item, the phase, and the ceiling — never the
    truncated content. Best-effort: the egress never raises, and a failure here must
    never mask the recorded failure the caller returns.
    """
    subject = task.display_subject()
    named_phase = phase or task.phase
    turns = message.num_turns if message is not None else 0
    logger.error(
        "Task %s (%s) stopped at its per-run turn ceiling after %s turns in phase %s — no result envelope",
        task.pk,
        subject,
        turns,
        named_phase,
    )
    text = (
        f"Run stopped at its per-run turn ceiling after {turns} turns on {subject} "
        f"(phase `{named_phase}`). The phase ended between turns without a result envelope "
        "and was recorded FAILED — raise `headless_max_turns` (claude_sdk lane) or "
        "`pydantic_ai_request_limit` (pydantic_ai lane) if this recurs."
    )
    try:
        notify_user(
            text,
            kind=NotifyKind.INFO,
            idempotency_key=f"max-turns-truncation:{task.pk}:{named_phase}",
            audience=NotifyAudience.OWNER_ESCALATION,
        )
    except Exception:
        logger.debug("turn-ceiling truncation owner alert failed for task %s", task.pk, exc_info=True)
