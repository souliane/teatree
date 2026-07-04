"""Review-state gate on the review-request broadcast (PR-08).

The hole this forecloses: a review-request broadcast goes out for a ticket
whose FSM never reached REVIEWED (no cold review ran) — colleagues are pinged
to review work that was not itself reviewed first. Skill prose says "review
before you request review", but nothing mechanically refuses the broadcast.

This is the structural gate. A broadcast is refused unless BOTH hold:

* the ticket's FSM state is ``REVIEWED``, and
* a recorded review-evidence artifact exists — a
    :class:`~teatree.core.models.review_evidence.ReviewEvidence` cold-review
    row, OR an existing
    :class:`~teatree.core.models.review_verdict.ReviewVerdict` for the ticket.
    Accepting the verdict keeps the artifact **recordable by the cold-review
    step**: that step already records a ``ReviewVerdict``, so a normal
    reviewed-and-cleared flow satisfies the gate with no extra step.

``require_reviewed_state_for_review_request`` is ``False`` unless configured —
with it unset the gate is a NO-OP, so a project that does not require it keeps
requesting review unchanged. The gate is a pure function over durable state; on
a block it returns a non-empty refusal string (the review-request post command
surfaces it as a non-zero exit), mirroring
:mod:`teatree.core.gates.anti_vacuity_gate`.

Caveat — the ``ReviewVerdict`` bridge only fires when the verdict is bound to
*this* ticket. :func:`has_review_evidence` matches a ``ReviewVerdict`` via
``filter(ticket=ticket)``, so a cold review satisfies the gate with no extra
``record-evidence`` step **only if** its verdict was recorded with
``review record … --ticket-id <ticket>``. A verdict recorded without
``--ticket-id`` (e.g. the auto-review-dispatch contract in
:func:`teatree.core.models.auto_review_dispatch.build_review_contract`, which
anchors a reviewer ticket rather than the work ticket) leaves ``ticket`` unset,
so it does NOT satisfy this gate — record a ``record-evidence --kind cold_review``
for the work ticket, or bind the verdict with ``--ticket-id``.
"""

from teatree.config import get_effective_settings
from teatree.core.models import ReviewEvidence, ReviewVerdict
from teatree.core.models.ticket import Ticket


def reviewed_state_required() -> bool:
    """Whether the review-state gate is in force (overlay -> global)."""
    return get_effective_settings().require_reviewed_state_for_review_request


def has_review_evidence(ticket: "Ticket") -> bool:
    """Whether a review-evidence artifact exists for the ticket.

    True when a ``ReviewEvidence`` cold-review row exists, OR an existing
    ``ReviewVerdict`` for the ticket does — the cold-review step records the
    latter, so this bridge keeps the evidence recordable by that step without
    changing it.
    """
    if ReviewEvidence.objects.has_cold_review(ticket):
        return True
    return ReviewVerdict.objects.filter(ticket=ticket).exists()


def check_reviewed_state(ticket: "Ticket") -> str:
    """Return a non-empty refusal when the review-request may not broadcast.

    NO-OP (returns ``""``) when ``require_reviewed_state_for_review_request``
    is off. Otherwise refuses — naming the missing precondition — unless the
    ticket is ``REVIEWED`` AND a review-evidence artifact exists.
    """
    if not reviewed_state_required():
        return ""

    if ticket.state != Ticket.State.REVIEWED:
        return (
            f"request review refused (require_reviewed_state_for_review_request): ticket {ticket.pk} is "
            f"in state {ticket.state!r}, not REVIEWED — a cold review must run and the ticket reach "
            f"REVIEWED before its review request broadcasts. Advance it through review first."
        )
    if not has_review_evidence(ticket):
        return (
            f"request review refused (require_reviewed_state_for_review_request): ticket {ticket.pk} is "
            f"REVIEWED but has no recorded review-evidence artifact. Record one with "
            f"`t3 <overlay> review record-evidence {ticket.pk} --kind cold_review --reviewer <id> "
            f"--verdict <merge_safe|hold> --head-sha <full-40-char-sha>` (the cold-review step's "
            f"ReviewVerdict also satisfies this), then retry."
        )
    return ""
