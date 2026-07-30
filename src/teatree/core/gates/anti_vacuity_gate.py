"""Anti-vacuity attestation gate on the review-request and merge transitions (#1829).

The hole this forecloses: a maker can request colleague review (or merge) an MR
whose new regression test is *vacuous* — it passes green even with the
production bug present (the failing case is skipped by a ``>= N`` guard, a
first-iteration skip, or an assertion on a structurally-guaranteed
post-condition the buggy code also satisfies). Colleagues review shallowly, so
a fundamentally-wrong MR sent to them ships. Skill prose + an eval state the
bar (correctness is the maker's responsibility), but neither mechanically
refuses the transition when the anti-vacuity proof is missing.

This is the structural gate. It extends the cold-review-before-merge CLEAR
machinery (``teatree.core.models.merge_clear``) with two dimensions, using the
same SHA-binding mechanism. (a) AC coverage — the diff was mapped against the
ticket/spec acceptance criteria. (b) Test anti-vacuity — for each new regression
test in the diff, a revert-fix -> RED proof was produced; a test that stays green
with the fix reverted guards nothing. An empty proof list is accepted ONLY when
the attester explicitly asserts the diff adds no new regression test
(``no_new_tests``), so a forgotten test can never pass as "none to prove".

SHA-binding is the distinguishing feature. The attestation records the
``head_sha`` it was produced against. When the live head moves off it
(force-push, new commits), the recorded attestation is treated as absent — a new
revision must be re-attested. This mirrors ``MergeClear.reviewed_sha`` and closes
the replay window where an attestation for an old, reviewed tree authorizes a
later, unreviewed one.

``require_anti_vacuity_attestation`` is ``False`` unless configured. With it
unset the gate is a NO-OP — projects that do not require the proof keep
requesting review / merging unchanged.

The gate is a pure function over durable ``extra`` state plus the live head
SHA, mirroring ``teatree.core.gates.review_skill_gate`` /
``teatree.core.gates.review_context_gate``. On a block it raises
:class:`AntiVacuityAttestationError` with a remediation message naming the
``record-anti-vacuity`` command; callers (the merge precondition gate and the
review-request post command) surface it as a non-zero exit / refusal.
"""

from typing import TYPE_CHECKING

from teatree.config import get_effective_settings
from teatree.core.models.types import AntiVacuityAttestation

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


class AntiVacuityAttestationError(RuntimeError):
    """A review-request / merge lacked a SHA-bound anti-vacuity attestation."""


def anti_vacuity_required(overlay: str | None = None) -> bool:
    """Whether the anti-vacuity gate is in force for *overlay* (overlay -> global).

    *overlay* threads the ticket's own overlay so a per-overlay opt-in binds even
    when the evaluating process has no ambient ``T3_OVERLAY_NAME`` (the merge
    keystone runs env-less). ``None`` resolves the ambient overlay as before.
    """
    return get_effective_settings(overlay).require_anti_vacuity_attestation


def recorded_attestation(ticket: "Ticket") -> AntiVacuityAttestation:
    """The recorded anti-vacuity attestation, or an empty mapping."""
    raw = (ticket.extra or {}).get("anti_vacuity_attestation") or {}
    return AntiVacuityAttestation(**{k: v for k, v in raw.items() if k in AntiVacuityAttestation.__annotations__})


def is_complete(attestation: AntiVacuityAttestation) -> bool:
    """Whether an attestation records a genuine AC-mapping + anti-vacuity proof.

    A genuine attestation names how the diff was mapped to the acceptance
    criteria AND either lists at least one proven-anti-vacuous regression test
    or explicitly asserts no new regression test exists. An empty
    ``proven_tests`` with ``no_new_tests`` unset does not satisfy the gate — a
    forgotten test must never pass as "none to prove". This does not check the
    SHA bind; :func:`is_bound_to` does.
    """
    ac_coverage = str(attestation.get("ac_coverage", "")).strip()
    proven = attestation.get("proven_tests") or []
    has_proven = isinstance(proven, list) and any(str(t).strip() for t in proven)
    no_new_tests = bool(attestation.get("no_new_tests"))
    return bool(ac_coverage) and (has_proven or no_new_tests)


def is_bound_to(attestation: AntiVacuityAttestation, head_sha: str) -> bool:
    """Whether the attestation was produced against ``head_sha`` (the SHA bind).

    The compare is case-insensitive on the stripped SHA so a mixed-case
    ``head_sha`` from a forge ``headRefOid`` cannot silently miss. An empty
    recorded or presented SHA never matches — an unbound attestation is stale
    by construction.
    """
    recorded = str(attestation.get("head_sha", "")).strip().lower()
    presented = head_sha.strip().lower()
    return bool(recorded) and bool(presented) and recorded == presented


def check_anti_vacuity_attestation(ticket: "Ticket", head_sha: str, *, transition: str) -> None:
    """Refuse a ``transition`` lacking a SHA-bound, complete anti-vacuity attestation.

    NO-OP when ``require_anti_vacuity_attestation`` is off (the opt-in
    default). Otherwise the durable ``anti_vacuity_attestation`` artifact must
    be complete (AC-mapping + anti-vacuity proof) AND bound to ``head_sha`` —
    a stale-SHA attestation is treated as absent so a new revision is
    re-attested. ``transition`` names the gated action (e.g. ``"request
    review"`` / ``"merge"``) for the remediation message.
    """
    if not anti_vacuity_required(ticket.overlay or None):
        return
    attestation = recorded_attestation(ticket)
    if is_complete(attestation) and is_bound_to(attestation, head_sha):
        return
    reason = _block_reason(attestation, head_sha)
    short_sha = head_sha.strip()[:8] or head_sha.strip()
    msg = (
        f"refusing the '{transition}' transition for ticket {ticket.pk} at head "
        f"{short_sha}: {reason} (require_anti_vacuity_attestation). The maker's "
        f"skilled self-review must prove the diff is AC-mapped and every NEW "
        f"regression test is anti-vacuous (revert the production fix -> the test "
        f"goes RED). Record it with `lifecycle record-anti-vacuity {ticket.pk} "
        f"--head-sha <full-40-char-sha> --ac-coverage <how-the-diff-maps-to-the-AC> "
        f"--proven-test <test::id> [--proven-test ...]` (or `--no-new-tests` when "
        f"the diff genuinely adds no regression test), then retry."
    )
    raise AntiVacuityAttestationError(msg)


def _block_reason(attestation: AntiVacuityAttestation, head_sha: str) -> str:
    """The precise why-blocked clause for the remediation message."""
    if not attestation:
        return "no anti-vacuity attestation is recorded"
    if not is_complete(attestation):
        return (
            "the recorded attestation is incomplete (missing AC-coverage, or an "
            "empty proven-tests list without the explicit no-new-tests claim)"
        )
    recorded = str(attestation.get("head_sha", "")).strip()
    return (
        f"the recorded attestation binds to head {recorded[:8] or recorded!r}, not "
        f"the current head {head_sha.strip()[:8] or head_sha.strip()!r} — it is "
        f"stale (force-push / new commits); re-attest at the current SHA"
    )
