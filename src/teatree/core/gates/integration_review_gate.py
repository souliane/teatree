"""Cross-repo integration-review DoD gate on ``mark_delivered`` (PR-08).

The hole this forecloses: a ticket that touches ≥ 2 repos is closed while each
repo's PR was reviewed in isolation and the *combined* cross-repo changeset was
never reviewed as a whole — an integration regression (a producer/consumer
contract change split across repos) ships unseen. Per-PR cold review does not
cover the seam between the repos.

This is the structural gate. When a ticket's ``repos`` name ≥ 2 distinct repos,
``mark_delivered`` (RETROSPECTED -> DELIVERED — the "done"/close transition)
refuses unless an integration-review
:class:`~teatree.core.models.review_evidence.ReviewEvidence` row covers every
repo in the combined changeset. A single-repo ticket has no seam, so the gate
never fires for it — it can never block existing single-repo work.

``require_integration_review`` is ``False`` unless configured — with it unset
the gate is a NO-OP. ``ticket.extra['integration_review_override']`` with a
non-empty ``reason`` is the audited escape hatch so a legitimately-exempt ticket
is never hard-trapped, mirroring
:mod:`teatree.core.gates.fix_dod_gate`. The gate is a pure function over durable
state; on a block it raises :class:`IntegrationReviewError` so the transition
caller's outer atomic rolls the advance back and the FSM stays put.
"""

import logging
from typing import TYPE_CHECKING

from teatree.config import get_effective_settings
from teatree.core.modelkit.gate_registry import register_gate
from teatree.core.models import ReviewEvidence
from teatree.core.models.errors import InvalidTransitionError

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)

_MIN_CROSS_REPO = 2


class IntegrationReviewError(InvalidTransitionError):
    """A ≥ 2-repo ticket close was refused: no integration review of the combined changeset.

    A subclass of :class:`InvalidTransitionError` (sibling of
    :class:`~teatree.core.gates.fix_dod_gate.FixRecordDodError`) so the caller's
    outer atomic rolls the close advance back and the FSM stays put. The message
    names the record-evidence command and the override escape hatch.
    """


def integration_review_required(overlay: str | None = None) -> bool:
    """Whether the integration-review gate is in force for *overlay* (overlay -> global).

    *overlay* threads the ticket's own overlay so a per-overlay opt-in binds even
    when the evaluating process has no ambient ``T3_OVERLAY_NAME``. ``None``
    resolves the ambient overlay as before.
    """
    return get_effective_settings(overlay).require_integration_review


def distinct_repos(ticket: "Ticket") -> list[str]:
    """The ticket's distinct, non-blank repo identifiers, first-seen order."""
    seen: dict[str, None] = {}
    for repo in ticket.repos or []:
        cleaned = str(repo).strip()
        if cleaned and cleaned not in seen:
            seen[cleaned] = None
    return list(seen)


def override_reason(ticket: "Ticket") -> str:
    """The recorded escape-hatch reason, or ``""`` when no override is set."""
    override = (ticket.extra or {}).get("integration_review_override") or {}
    return str(override.get("reason", "")).strip()


def check_integration_review(ticket: "Ticket") -> None:
    """Refuse the close transition when a ≥ 2-repo ticket lacks an integration review.

    Order of short-circuits (cheapest, most-permissive first):

    1. Gate off (``require_integration_review`` unset) → pass.
    2. Fewer than 2 distinct repos → pass (no cross-repo seam to review).
    3. A recorded override reason → pass (logged for audit).
    4. An integration-review artifact covering every repo → pass.
    5. Otherwise → raise :class:`IntegrationReviewError`.
    """
    if not integration_review_required(ticket.overlay or None):
        return
    repos = distinct_repos(ticket)
    if len(repos) < _MIN_CROSS_REPO:
        return
    reason = override_reason(ticket)
    if reason:
        logger.info("Integration-review gate overridden for ticket %s: %s", ticket.pk, reason)
        return

    if ReviewEvidence.objects.has_integration_review_covering(ticket, repos):
        return
    msg = (
        f"Refusing to close ticket {ticket} — it touches {len(repos)} repos ({', '.join(repos)}) so its "
        f"Definition of Done requires an integration review covering the COMBINED changeset, and none is "
        f"recorded. Per-repo review does not cover the cross-repo seam. Record it with "
        f"`t3 <overlay> review record-evidence {ticket.pk} --kind integration_review --reviewer <id> "
        f"--verdict <pass|hold> --head-sha <full-40-char-sha> "
        f"--repos {','.join(repos)}`. If genuinely exempt, record an override: "
        f"`t3 <overlay> ticket integration-review-override {ticket.pk} --reason '<why>'`."
    )
    raise IntegrationReviewError(msg)


register_gate("integration_review", check_integration_review)
