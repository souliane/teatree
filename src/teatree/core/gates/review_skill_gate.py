"""Reviewing-phase evidence gate: the `reviewing` attestation needs review-skill proof (#1539).

The hole this forecloses: ``lifecycle visit-phase <id> reviewing`` records the
independent-review attestation, but the existing reviewer-identity gate only
proves *who* recorded it — not that the configured deep-review skill actually
ran. When a project opts in by configuring ``review_skill`` (env
``T3_REVIEW_SKILL``, per-overlay, or global ``[teatree]``), the reviewing
attestation must be backed by a durable ``review_skill_run`` artifact naming
that skill.

Opt-in default
    ``review_skill`` is empty unless configured. With no skill configured the
    gate is a NO-OP — projects that do not use a review skill keep recording
    ``reviewing`` unchanged.

Satisfying evidence
    ``ticket.extra['review_skill_run']`` whose ``skill`` equals the currently
    configured ``review_skill``. Evidence for a different (e.g. stale) skill
    does not satisfy the gate — the artifact must attest the skill in force.

The gate is a pure function over durable ``extra`` state, mirroring
``teatree.core.gates.dod_gate``. On a block it raises
:class:`ReviewSkillEvidenceError` with a remediation message naming the
expected evidence; the ``visit-phase`` command surfaces it as a non-zero exit.
"""

from typing import TYPE_CHECKING

from teatree.config import get_effective_settings

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


class ReviewSkillEvidenceError(RuntimeError):
    """A ``reviewing`` visit lacked evidence the configured review skill ran."""


def configured_review_skill() -> str:
    """The effective ``review_skill`` (env -> per-overlay -> global -> default)."""
    return get_effective_settings().review_skill.strip()


def recorded_review_skill(ticket: "Ticket") -> str:
    """The skill name recorded by the latest review-skill run, or ``""``."""
    run = (ticket.extra or {}).get("review_skill_run") or {}
    return str(run.get("skill", "")).strip()


def check_review_skill_evidence(ticket: "Ticket") -> None:
    """Refuse a ``reviewing`` attestation that no review-skill run backs.

    NO-OP when ``review_skill`` is unset (the opt-in default). Otherwise the
    durable ``review_skill_run`` artifact must name the configured skill.
    """
    expected = configured_review_skill()
    if not expected:
        return
    if recorded_review_skill(ticket) == expected:
        return
    msg = (
        f"`lifecycle visit-phase {ticket.pk} reviewing` requires evidence that "
        f"the configured review skill {expected!r} ran (T3_REVIEW_SKILL / "
        f"review_skill). Run `/{expected}`, then record it with "
        f"`lifecycle record-review-skill-run {ticket.pk} {expected}` and retry."
    )
    raise ReviewSkillEvidenceError(msg)
