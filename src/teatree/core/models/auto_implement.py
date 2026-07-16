"""Auto-implement marker for the issue-implementer direct-coding flow.

``persistence._handle_orchestrator`` schedules a ``coding`` task directly on a
freshly-created NOT_STARTED author ticket — the issue-implementer auto-start
path deliberately skips the scope/plan phases. When that coding task completes
the ticket is still in an early state, so the normal ``coding -> code()`` guard
(``source=PLANNED``) cannot fire and :meth:`Task._apply_phase_transition` would
silently no-op (the wedge that left tickets 35/36 with completed coding yet zero
transitions and no PR).

This marker, stamped on ``Ticket.extra`` via the canonical locked
``merge_extra``, records that the ticket is on the plan-skipped direct-coding
path, so the coding-completion transition path advances it via
``Ticket.code_direct`` from an early state — reachable ONLY for a marked ticket,
so the normal author flow's plan gate is never weakened.

Lives at module scope (not on ``Ticket``) to keep the model under the
module-health LOC cap; semantically a sibling of ``external_delivery`` and
``trivial_plan_skip``.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

_MARKER_KEY = "auto_implement"


def mark_auto_implement(ticket: "Ticket") -> None:
    """Mark *ticket* as the plan-skipped issue-implementer direct-coding flow.

    Written through the ticket's canonical locked ``merge_extra`` so a
    concurrent ``extra`` writer's key survives.
    """
    ticket.merge_extra(set_keys={_MARKER_KEY: True})


def is_auto_implement(ticket: object) -> bool:
    """Whether *ticket* carries the auto-implement direct-coding marker.

    Typed ``object`` (like the sibling ``ticket._check_plan_artifact`` predicate)
    so it slots directly into a ``django_fsm`` ``@transition`` ``conditions=[...]``
    list — whose contract is ``Callable[[Model], bool]`` — without a wrapper
    lambda. Fail-safe: a missing or malformed ``extra`` reads as ``False`` (not
    auto-implement), so a garbled row can never widen the ``code_direct`` gate.
    """
    extra = getattr(ticket, "extra", None)
    extra = extra if isinstance(extra, dict) else {}
    return extra.get(_MARKER_KEY) is True
