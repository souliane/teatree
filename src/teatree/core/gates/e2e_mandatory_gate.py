"""Mandatory-E2E FSM gate for customer-display-impacting changes (#1967).

User directive (recurring): E2E tests are mandatory for anything that could
impact what is displayed to the customer — and a backend change usually does.
The prose rules (code-skill §5, review-skill checklist) did not hold at volume,
so this is the deterministic substitute: a customer-display-impacting change
cannot ship or be CLEARed for merge without green E2E evidence that is both
SHA-bound and POSTED — a recorded-but-unposted run does NOT satisfy the gate
("recorded e2e evidence is NOT enough — it must be posted too") — and the only
bypass requires explicit user approval, never the implementing agent's own
judgment.

The gate is a pure decision over durable state — the classifier verdict, the
``E2eMandatoryRun`` evidence rows (whose ``posted_url`` proves the evidence was
posted), and the ``E2EBypassApproval`` rows — keyed on
the exact reviewed tree (``head_sha``). It mirrors the satisfiable-but-not-
suppressible shape of the on-behalf gate
(:mod:`teatree.core.on_behalf_gate_recorded`) and ``MergeClear``:

*   not display-impacting → pass (the gate only governs user-visible work); a
    diff that could not be read is presumed impacting and, if nothing below
    covers the tree, the gate DID NOT RUN rather than refusing on a classification;
*   the ticket's repo ships no customer display surface at all → pass (a
    repo-level exemption the overlay declares, for a repo whose every file is
    non-impacting by construction — a skills/docs repo — so no glob list has to
    keep up with each new fixture kind it grows);
*   a green AND posted ``E2eMandatoryRun`` at ``head_sha`` → pass;
*   an unconsumed ``E2EBypassApproval`` at ``(ticket, head_sha)`` → consume it
    single-use inside one ``transaction.atomic`` block, write an
    ``E2EBypassAudit`` row, and pass;
*   otherwise → raise :class:`E2EMandatoryGateError`, whose message names BOTH
    remedies verbatim (the record-e2e-run command and the e2e-bypass command).

:func:`check_e2e_mandatory` is the consuming entry point (it may claim a bypass);
:func:`e2e_mandatory_verdict` maps its outcome onto the gate-verdict vocabulary
both gate sites render.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from teatree.core.modelkit.gate_verdict import Pass, Refuse, Unknown, Verdict, evaluate_gate, guarded_read
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.ticket import Ticket
from teatree.core.overlay_loader import get_overlay
from teatree.utils.url_slug import slug_from_issue_or_pr_url

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase


class E2EMandatoryGateError(InvalidTransitionError):
    """A ship / CLEAR was refused: a display-impacting change has no E2E evidence.

    A subclass of :class:`InvalidTransitionError` (sibling of
    ``DodLocalE2EError``) so a ship transition that hits it rolls back and the
    FSM stays put. The message names both satisfiers so the operator can
    unblock without code.
    """


@dataclass(frozen=True, slots=True)
class GateInputs:
    """The mandatory-E2E gate's inputs, resolved once and passed as a unit.

    ``display_impacting`` is the overlay's verdict over ``changed_files`` —
    ``False`` when the ticket's repo carries no customer display surface at all,
    otherwise the per-path classifier's answer, and ``True`` when the diff could
    not be read (``unread_diff`` then says why); ``head_sha`` is the reviewed
    tree the evidence/bypass bind to.
    """

    ticket: Ticket
    changed_files: list[str]
    head_sha: str
    display_impacting: bool
    unread_diff: str = ""


def _repo_has_no_display_surface(overlay: "OverlayBase", ticket: Ticket) -> bool:
    """Whether *ticket*'s repo is one the overlay declares wholly free of customer display surface.

    The repo is read off ``ticket.issue_url`` — the only repo identity the gate
    holds at both of its call sites — and an unparsable or absent URL yields no
    slug, which matches no declaration and so keeps the per-path classifier.
    """
    slug = slug_from_issue_or_pr_url(urlparse(ticket.issue_url or "").path)
    return bool(slug) and slug in overlay.review.mandatory_e2e_exempt_repo_slugs()


def resolve_gate_inputs(ticket: Ticket, *, read_diff: Callable[[], list[str]], head_sha: str) -> GateInputs:
    """Build :class:`GateInputs` for *ticket* at the reviewed *head_sha*.

    The wiring seam the ship-gate and §17.4 CLEAR call: it asks the active
    overlay to classify the diff *read_diff* returns (fail-closed default). The
    caller supplies the diff reader + head SHA so this function stays free of git
    I/O and is exhaustively testable. A diff the reader cannot produce is never
    classified as an empty one: it is presumed impacting and recorded on
    ``unread_diff``.

    Fails CLOSED on an unresolvable overlay (#1426 posture): when
    ``ticket.overlay`` cannot be resolved to a registered overlay, the change is
    presumed display-impacting so the gate is never silently skipped by a
    misconfigured ticket. Posted evidence or a user bypass satisfies the gate.

    A repo the overlay declares display-surface-free short-circuits the path
    classifier, and reads no diff: no file in it can reach a customer screen, so
    no diff in it can. Every other repo still goes through the fail-closed
    per-path classifier.
    """
    from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415 — deferred: Django import at call time

    try:
        overlay = get_overlay(ticket.overlay or None)
    except ImproperlyConfigured:
        overlay = None
    if overlay is not None and _repo_has_no_display_surface(overlay, ticket):
        return GateInputs(ticket=ticket, changed_files=[], head_sha=head_sha, display_impacting=False)
    diff = guarded_read("the changed-file diff", read_diff, neutral=[])
    if diff.failed:
        return GateInputs(
            ticket=ticket,
            changed_files=[],
            head_sha=head_sha,
            display_impacting=True,
            unread_diff=f"{type(diff.error).__name__}: {diff.error}",
        )
    return GateInputs(
        ticket=ticket,
        changed_files=diff.value,
        head_sha=head_sha,
        display_impacting=overlay is None or overlay.review.classify_customer_display_impact(diff.value),
    )


_DIFF_PATHS_SHOWN = 10


def _diff_summary(inputs: GateInputs) -> str:
    """The diff the verdict was computed over, so the reader never has to guess it."""
    if not inputs.changed_files:
        return "the diff enumerated no files"
    shown = inputs.changed_files[:_DIFF_PATHS_SHOWN]
    elided = len(inputs.changed_files) - len(shown)
    return ", ".join(shown) + (f" (+{elided} more)" if elided else "")


def _deny_message(inputs: GateInputs) -> str:
    if inputs.unread_diff:
        verdict = (
            f"the changed files of ticket {inputs.ticket.pk} at the reviewed tree {inputs.head_sha[:8]} could not "
            f"be read ({inputs.unread_diff}), so whether the change can reach a customer display is unknown, and "
            f"no green PUBLISHED E2E evidence covers that tree. Repair the worktree so its diff reads and retry, "
            f"or satisfy the gate with EITHER:\n"
        )
    else:
        verdict = (
            f"Refusing to ship/CLEAR ticket {inputs.ticket.pk}: the change classified customer-display-"
            f"impacting and has no green PUBLISHED E2E evidence at the reviewed tree {inputs.head_sha[:8]}. "
            f"The verdict is FAIL-CLOSED and says only that a changed path did not match the overlay's "
            f"non-impacting allowlist — NOT that a serializer / view / frontend / template / "
            f"document-generation file is present, since an unanticipated path reads identically. "
            f"The diff at that tree: {_diff_summary(inputs)}. E2E is a mandatory FSM "
            f"step for anything that could impact what is displayed to the customer, and a run recorded only in "
            f"the teatree DB is not enough — the evidence must live where reviewers read it (#1967). Satisfy the "
            f"gate with EITHER:\n"
        )
    return (
        verdict + f"  1. write the test plan:  t3 <overlay> e2e write-test-plan --ticket {inputs.ticket.pk} "
        f"--manifest <manifest.json>\n"
        f"     then attest it:  t3 <overlay> lifecycle record-e2e-run {inputs.ticket.pk} --spec <path> "
        f"--result green --head-sha {inputs.head_sha} --posted-url test-plans/<repo>-<ticket>.md\n"
        f"  2. OR a user bypass: t3 <overlay> ticket e2e-bypass {inputs.ticket.pk} --approver <user-id> "
        f"--head-sha {inputs.head_sha}\n"
        f"The bypass requires explicit user approval — a maker/coding-agent/loop id is refused. Without "
        f"either, E2E stays mandatory."
    )


def _passes_without_bypass(inputs: GateInputs) -> bool:
    """True iff the gate passes without needing to consume a bypass.

    The cheap, most-permissive short-circuits first: not display-impacting or
    green evidence at the reviewed tree.
    """
    from teatree.core.models.e2e_mandatory_run import E2eMandatoryRun  # noqa: PLC0415 — deferred: ORM/app-registry

    if not inputs.display_impacting:
        return True
    return E2eMandatoryRun.has_green_evidence(inputs.ticket, inputs.head_sha)


def check_e2e_mandatory(inputs: GateInputs) -> None:
    """Refuse a display-impacting ship/CLEAR without E2E evidence or a user bypass.

    Passes silently when the gate is satisfied. When the only satisfier is a
    recorded bypass, it is consumed single-use inside one ``transaction.atomic``
    block together with the audit write — so a concurrent second evaluation
    cannot reuse it. Raises :class:`E2EMandatoryGateError` (naming both
    remedies) when nothing satisfies it.
    """
    if _passes_without_bypass(inputs):
        return

    from django.db import transaction  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.core.models.e2e_bypass import E2EBypassApproval, E2EBypassAudit  # noqa: PLC0415 — lazy ORM import

    with transaction.atomic():
        consumed = E2EBypassApproval.consume(inputs.ticket, inputs.head_sha)
        if consumed is None:
            raise E2EMandatoryGateError(_deny_message(inputs))
        E2EBypassAudit.objects.create(
            approval=consumed,
            ticket=inputs.ticket,
            head_sha=consumed.head_sha,
            approver_id=consumed.approver_id,
        )


def e2e_mandatory_verdict(inputs: GateInputs) -> Verdict:
    """Judge *inputs*: a refusal over a diff nobody read is :class:`Unknown`, never a classified refusal."""
    try:
        check_e2e_mandatory(inputs)
    except E2EMandatoryGateError as exc:
        return Unknown(str(exc)) if inputs.unread_diff else Refuse(str(exc))
    return Pass()


def evaluate_e2e_mandatory(ticket: Ticket, *, read_head: Callable[[], str], read_diff: Callable[[], list[str]]) -> str:
    """Run the mandatory-E2E gate at both sites; return the rendered refusal, or ``""`` when it passed.

    The ship site reads HEAD from its worktree; the §17.4 CLEAR site (#1967) binds
    to its ``reviewed_sha``. Either reader failing means the gate did not run. A
    recorded bypass is consumed single-use here.
    """
    result = evaluate_gate(
        "e2e_mandatory",
        collect=lambda: resolve_gate_inputs(ticket, read_diff=read_diff, head_sha=read_head()),
        judge=e2e_mandatory_verdict,
    )
    return "" if result.passed else result.render()
