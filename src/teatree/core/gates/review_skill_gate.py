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
    ``ticket.extra['review_skill_run']`` whose ``skill`` is one of the currently
    ACCEPTED skills — the configured ``review_skill`` plus any
    ``review_skill_alternates``. Evidence for a skill outside that set (e.g. a
    stale one) does not satisfy the gate; the artifact must attest a review the
    project actually recognises.

    Alternates are SUBSTITUTES, never a bypass. An overlay that runs one deep
    reviewer by default but accepts a second — a different model's reviewer, say —
    declares it here so either recorded run passes, while a run of NEITHER stays
    refused. The gate's teeth are unchanged; only the set of reviewers that can
    satisfy it widens. With ``review_skill`` unset the gate stays a NO-OP whatever
    the alternates say: alternates alone never arm it.

Repo scoping
    The gate only applies to a ticket whose issue lives in its overlay's OWN
    primary repo(s) (:func:`~teatree.core.overlay_loader.ticket_repo_is_overlay_own`).
    A ticket reached only through the overlay's broader workspace-repo
    routing — a sibling project's own repo-ownership config doesn't
    enumerate its own meta/tooling repo, so its tickets dispatch through
    this overlay's workspace-repo list purely for administrative
    convenience — is exempt, since the ticket isn't actually this overlay's
    own codebase (#1539 / #2895).

The gate is a pure function over durable ``extra`` state, mirroring
``teatree.core.gates.dod_gate``. On a block it raises
:class:`ReviewSkillEvidenceError` with a remediation message naming the
expected evidence; the ``visit-phase`` command surfaces it as a non-zero exit.
"""

from typing import TYPE_CHECKING

from teatree.config import get_effective_settings
from teatree.core.overlay_loader import ticket_repo_is_overlay_own

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


class ReviewSkillEvidenceError(RuntimeError):
    """A ``reviewing`` visit lacked evidence the configured review skill ran."""


def configured_review_skill(overlay: str | None = None) -> str:
    """The effective ``review_skill`` (env -> per-overlay -> global -> default)."""
    return get_effective_settings(overlay).review_skill.strip()


def configured_review_skill_alternates(overlay: str | None = None) -> tuple[str, ...]:
    """The effective ``review_skill_alternates``, stripped, deduped, order-preserving."""
    declared = get_effective_settings(overlay).review_skill_alternates
    return tuple(dict.fromkeys(name.strip() for name in declared if name.strip()))


#: The per-PR review tier a single ship's `reviewing` attestation is evidence of.
PER_PR_REVIEW_SKILL = "t3:review"


def accepted_per_pr_review_skills(overlay: str | None = None) -> frozenset[str]:
    """Every skill whose recorded run satisfies a per-PR ship (souliane/teatree#3530).

    ``review_skill`` set to the periodic architectural tier
    (``architectural_review_skill``) is a tier mismatch: that skill is a
    whole-tree sweep dispatched by ``ArchitecturalReviewScanner`` on a cadence,
    so its findings speak to the tree rather than to the diff being shipped, and
    demanding it per PR creates pressure to record a run that did not happen.
    The primary is scoped to the per-PR tier instead — still evidence-backed, just
    of the review a single ship actually performs.

    The alternates join that primary, so a project can accept more than one
    reviewer without dropping the requirement. An empty return means the gate is
    unarmed: only an unset ``review_skill`` produces it, so declaring alternates
    can never turn the gate off.
    """
    configured = configured_review_skill(overlay)
    if not configured:
        return frozenset()
    architectural = get_effective_settings(overlay).architectural_review_skill.strip()
    primary = PER_PR_REVIEW_SKILL if configured == architectural else configured
    return frozenset({primary, *configured_review_skill_alternates(overlay)})


def recorded_review_skill(ticket: "Ticket") -> str:
    """The skill name recorded by the latest review-skill run, or ``""``."""
    run = (ticket.extra or {}).get("review_skill_run") or {}
    return str(run.get("skill", "")).strip()


def check_review_skill_evidence(ticket: "Ticket") -> None:
    """Refuse a ``reviewing`` attestation that no accepted review-skill run backs.

    NO-OP when ``review_skill`` is unset for the TICKET's own overlay (the
    opt-in default — resolving the ambient process overlay instead would let
    another overlay's configuration disarm the gate, the #F2.3 hole) or when
    *ticket* isn't in its overlay's own repo (see "Repo scoping" above).
    Otherwise the durable ``review_skill_run`` artifact must name one of the
    accepted per-PR review skills (:func:`accepted_per_pr_review_skills`). The
    refusal names every one of them, so the operator can see which reviewers
    would satisfy it rather than guessing at a single expected string.
    """
    accepted = accepted_per_pr_review_skills(ticket.overlay or None)
    if not accepted:
        return
    if not ticket_repo_is_overlay_own(ticket):
        return
    if recorded_review_skill(ticket) in accepted:
        return
    names = sorted(accepted)
    msg = (
        f"`lifecycle visit-phase {ticket.pk} reviewing` requires evidence that one of "
        f"the accepted per-PR review skills ran (review_skill / review_skill_alternates): "
        f"{', '.join(repr(name) for name in names)}. Run `/{names[0]}` (or any of the "
        f"others), then record it with `lifecycle record-review-skill-run {ticket.pk} "
        f"<skill>` and retry."
    )
    raise ReviewSkillEvidenceError(msg)
