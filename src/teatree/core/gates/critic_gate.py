"""critic_gate: the autonomous user-proxy critic on ``mark_delivered`` (SELFCATCH-5).

This is the unifying runtime of the self-catching layer — the single chokepoint
where, at the FSM's FINAL done-claim (RETROSPECTED→DELIVERED), the critic re-asks
the adversarial questions the human had to ask all session: is this actually done?
is the plan a plan? was any input ignored? was the scope silently reduced? It walks
the seeded rubric (:mod:`teatree.core.critic_rubric`) and RECORDS a
:class:`~teatree.core.models.critic_finding.CriticFinding` per failing item.

Advisory-first (the whole point of v1)
    The critic ALWAYS runs and records — even dark — so it gathers real evidence
    on real deliveries this week. Whether a finding BLOCKS the delivery is the ONE
    thing ``critic_gate_live`` gates. OFF (default) is ADVISORY: findings recorded,
    RETROSPECTED→DELIVERED still proceeds — it ships dark and never wedges a ticket.
    ON is ENFORCING: findings recorded AND a
    :class:`~teatree.core.models.errors.CriticGateError` is raised, so the outer
    atomic rolls the advance back and the ticket stays RETROSPECTED.

Reuse, not re-implementation
    The mechanical predicates CALL the sibling gates — ``merge_evidence_gate``,
    ``plan_currency_gate``/``plan_adequacy``, ``spec_coverage_gate`` — so the
    done-not-done / thin-plan / silent-scope classes are decided by the same code
    that gates them elsewhere. By the time a normal ticket reaches ``mark_delivered``
    it already passed ``mark_merged`` (which wrote a keystone MergeAudit row), so
    the done-not-done merge-evidence check is a pure DB read with no forge probe —
    the probe fires only for a genuinely anomalous unmerged-but-retrospected ticket.

Anti-theater
    A predicate that RAISES is inconclusive — the gate records an
    ``instrumentation_gap`` (counted as a FAIL, per the plan's never-fake-green
    doctrine), never a silent pass. A now-clean item's stale finding from a prior
    run is DELETED, so the row set is the LATEST verdict, not an append-only log.
"""

import logging
from typing import TYPE_CHECKING

from teatree.config import get_effective_settings
from teatree.core.critic_rubric import CriticRubricItem, rubric_items
from teatree.core.modelkit.gate_registry import register_gate
from teatree.core.models.critic_finding import CriticFinding, CriticFindingSpec
from teatree.core.models.errors import CriticGateError

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)

_TRANSITION = "mark_delivered"


def critic_enforcement_live(overlay_name: str | None) -> bool:
    """Whether a critic FINDING blocks delivery for *overlay_name* (overlay -> global)."""
    return bool(get_effective_settings(overlay_name).critic_gate_live)


def _evaluate_item(ticket: "Ticket", item: CriticRubricItem) -> tuple[str, str] | None:
    """Return ``(status, detail)`` when *item* fails over *ticket*, else ``None``.

    ``(FAIL, detail)`` when the predicate CATCHES its failure class;
    ``(INSTRUMENTATION_GAP, why)`` when the predicate RAISES (inconclusive is a
    FAIL, never a silent pass). ``None`` when the item is clean.
    """
    try:
        detail = item.evaluate(ticket)
    except Exception as exc:  # noqa: BLE001 — an inconclusive predicate is a finding, not a crash of mark_delivered.
        logger.warning("critic rubric item %r raised over ticket %s: %s", item.slug, ticket.pk, exc)
        return CriticFinding.Status.INSTRUMENTATION_GAP, f"predicate raised (inconclusive): {exc}"
    if detail:
        return CriticFinding.Status.FAIL, detail
    return None


def run_critic(ticket: "Ticket") -> list[CriticFinding]:
    """Walk the rubric over *ticket*, upsert a finding per failing item, return the findings.

    Recording is UNCONDITIONAL (the advisory posture): a clean item's stale finding
    is deleted so the row set reflects the latest verdict. Pure of the enforcement
    decision — :func:`check_critic` decides whether the returned findings block.
    """
    head_sha = str((ticket.extra or {}).get("head_sha") or "")
    findings: list[CriticFinding] = []
    for item in rubric_items():
        outcome = _evaluate_item(ticket, item)
        if outcome is None:
            CriticFinding.objects.filter(ticket=ticket, transition=_TRANSITION, rubric_item=item.slug).delete()
            continue
        status, detail = outcome
        spec = CriticFindingSpec(
            rubric_item=item.slug,
            detail=detail,
            status=status,
            adversarial_question=item.adversarial_question,
            head_sha=head_sha,
        )
        findings.append(CriticFinding.record(ticket=ticket, transition=_TRANSITION, spec=spec))
    return findings


def check_critic(ticket: "Ticket") -> None:
    """Run the critic at ``mark_delivered``; block only when enforcement is live.

    Always records findings (advisory). When ``critic_gate_live`` is on for the
    ticket's overlay AND any finding exists, raises :class:`CriticGateError` so the
    delivery is refused and the ticket stays RETROSPECTED.
    """
    findings = run_critic(ticket)
    if not findings or not critic_enforcement_live(ticket.overlay or None):
        return
    items = ", ".join(f"{f.rubric_item} ({f.status})" for f in findings)
    msg = (
        f"Refusing to mark ticket {ticket.pk} DELIVERED — the critic found {len(findings)} unresolved "
        f"issue(s) the human would have had to point out: {items}. Each is recorded as a CriticFinding "
        f"naming the offending artifact — resolve them (or record the explicit reasoned negative), then "
        f"re-run delivery. If a finding is a genuine false positive the operator's audited escape is to "
        f"disable enforcement: `t3 <overlay> config_setting set critic_gate_live false --overlay <name>` "
        f"(advisory recording continues)."
    )
    raise CriticGateError(msg)


register_gate("critic", check_critic)
