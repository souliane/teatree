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
posted), the ``E2EBypassApproval`` rows, and the gate's kill-switch — keyed on
the exact reviewed tree (``head_sha``). It mirrors the satisfiable-but-not-
suppressible shape of the on-behalf gate
(:mod:`teatree.core.on_behalf_gate_recorded`) and ``MergeClear``:

*   not display-impacting → pass (the gate only governs user-visible work);
*   kill-switch off → pass (the operator's deliberate, audited opt-out);
*   a green AND posted ``E2eMandatoryRun`` at ``head_sha`` → pass;
*   an unconsumed ``E2EBypassApproval`` at ``(ticket, head_sha)`` → consume it
    single-use inside one ``transaction.atomic`` block, write an
    ``E2EBypassAudit`` row, and pass;
*   otherwise → raise :class:`E2EMandatoryGateError`, whose message names BOTH
    remedies verbatim (the record-e2e-run command and the e2e-bypass command).

:func:`check_e2e_mandatory` is the consuming entry point (it may claim a bypass).
:func:`e2e_mandatory_block_message` is the non-consuming peek a caller uses to
refuse early before expensive prep; the real pass then goes through
:func:`check_e2e_mandatory`, so a peek never burns a bypass.
"""

from dataclasses import dataclass

from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.ticket import Ticket
from teatree.core.overlay_loader import get_overlay


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

    ``display_impacting`` is the overlay classifier's verdict over
    ``changed_files``; ``head_sha`` is the reviewed tree the evidence/bypass
    bind to; ``gate_enabled`` is the resolved kill-switch.
    """

    ticket: Ticket
    changed_files: list[str]
    head_sha: str
    display_impacting: bool
    gate_enabled: bool = True


def _gate_enabled(overlay_name: str | None) -> bool:
    """Resolve the gate's OWN kill-switch (never another gate's switch).

    ``[teatree] e2e_mandatory_gate_enabled`` (per-overlay overridable via
    ``[overlays.<name>]``) defaults to ``True``.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415

    return bool(get_effective_settings(overlay_name).e2e_mandatory_gate_enabled)


def resolve_gate_inputs(ticket: Ticket, *, changed_files: list[str], head_sha: str) -> GateInputs:
    """Build :class:`GateInputs` for *ticket* at the reviewed *head_sha*.

    The wiring seam the ship-gate and §17.4 CLEAR call: it asks the active
    overlay to classify ``changed_files`` (fail-closed default) and resolves
    the gate kill-switch for the ticket's overlay. The caller supplies the
    already-resolved diff + head SHA so this function stays free of git I/O and
    is exhaustively testable.

    Fails CLOSED on an unresolvable overlay (#1426 posture): when
    ``ticket.overlay`` cannot be resolved to a registered overlay, the change is
    presumed display-impacting so the gate is never silently skipped by a
    misconfigured ticket. The evidence / bypass / kill-switch escapes keep this
    from being a hard lockout.
    """
    from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415

    try:
        overlay = get_overlay(ticket.overlay or None)
        display_impacting = overlay.classify_customer_display_impact(changed_files)
    except ImproperlyConfigured:
        display_impacting = True
    return GateInputs(
        ticket=ticket,
        changed_files=changed_files,
        head_sha=head_sha,
        display_impacting=display_impacting,
        gate_enabled=_gate_enabled(ticket.overlay or None),
    )


def _deny_message(inputs: GateInputs) -> str:
    return (
        f"Refusing to ship/CLEAR ticket {inputs.ticket.pk}: the change is customer-display-impacting "
        f"(a serializer / view / frontend / template / document-generation file is in the diff) but has "
        f"no green POSTED E2E evidence at the reviewed tree {inputs.head_sha[:8]}. E2E is a mandatory FSM "
        f"step for anything that could impact what is displayed to the customer, and recorded evidence is "
        f"not enough — it must be POSTED (#1967). Satisfy the gate with EITHER:\n"
        f"  1. post test plan:  t3 <overlay> e2e post-test-plan --ticket {inputs.ticket.pk} --env dev|local "
        f"--before <img> --after <img> --assertion <claim>\n"
        f"     then attest it:  t3 <overlay> lifecycle record-e2e-run {inputs.ticket.pk} --spec <path> "
        f"--result green --head-sha {inputs.head_sha} --posted-url <comment-url>\n"
        f"  2. OR a user bypass: t3 <overlay> ticket e2e-bypass {inputs.ticket.pk} --approver <user-id> "
        f"--head-sha {inputs.head_sha}\n"
        f"The bypass requires explicit user approval — a maker/coding-agent/loop id is refused. Without "
        f"either, E2E stays mandatory."
    )


def _passes_without_bypass(inputs: GateInputs) -> bool:
    """True iff the gate passes without needing to consume a bypass.

    The cheap, most-permissive short-circuits first: not display-impacting,
    kill-switch off, or green evidence at the reviewed tree.
    """
    from teatree.core.models.e2e_mandatory_run import E2eMandatoryRun  # noqa: PLC0415

    if not inputs.display_impacting:
        return True
    if not inputs.gate_enabled:
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

    from django.db import transaction  # noqa: PLC0415

    from teatree.core.models.e2e_bypass import E2EBypassApproval, E2EBypassAudit  # noqa: PLC0415

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


def check_clear_e2e_mandatory(ticket: Ticket | None, reviewed_sha: str, changed_files: list[str]) -> str:
    """Gate the §17.4 CLEAR on the mandatory-E2E requirement; return a refusal or ``""``.

    The second gate site (#1967). A ticket-bound CLEAR for a customer-display-
    impacting change is refused unless green E2E evidence exists at
    ``reviewed_sha`` OR a single-use user bypass exists OR the kill-switch is
    off; a recorded bypass is consumed single-use here. An out-of-FSM CLEAR
    (``ticket`` is ``None``) is not gated — the gate binds to a ticket's
    evidence and has no ticket to bind to.

    ``changed_files`` is resolved by the caller (the command layer, which may
    reach the integration-layer git diff helper); the gate stays in the domain
    layer over pure inputs. An empty ``changed_files`` is fail-closed impacting
    for a customer-facing overlay, so a ticket-bound CLEAR still requires
    evidence rather than silently skipping.
    """
    if ticket is None:
        return ""

    inputs = resolve_gate_inputs(ticket, changed_files=changed_files, head_sha=reviewed_sha)
    try:
        check_e2e_mandatory(inputs)
    except E2EMandatoryGateError as exc:
        return str(exc)
    return ""


def e2e_mandatory_block_message(inputs: GateInputs) -> str:
    """Return the deny message, or ``""`` when the gate would pass — non-consuming.

    The peek behind an early refusal: it reports whether
    :func:`check_e2e_mandatory` would raise, without consuming a bypass or
    writing an audit. A pending unconsumed bypass at ``(ticket, head_sha)``
    therefore reports ``""`` (would pass) but is left intact for the consuming
    call.
    """
    if _passes_without_bypass(inputs):
        return ""

    from teatree.core.models.e2e_bypass import E2EBypassApproval  # noqa: PLC0415

    if E2EBypassApproval.has_unconsumed(inputs.ticket, inputs.head_sha):
        return ""
    return _deny_message(inputs)
