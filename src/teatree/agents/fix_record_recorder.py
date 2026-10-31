"""Server-side persistence for a fixing agent's returned ``fix_record`` (#4520).

``core.gates.fix_dod_gate`` blocks every ``kind=fix`` ticket at DELIVERED unless
``ticket.extra['fix_record']`` is complete, and NOTHING wrote that key — the gate's
only route through was its own override, which made it decorative. This module is
the positive path: the fixing agent returns the record in its result envelope and
the recorder validates and writes it, rather than a CLI taking the agent's word for
it (the self-attestation ``core.models.repro_waiver`` already argues against).

Two decisions make this safe to add to a schema other overlays consume:

* an ABSENT ``fix_record`` is a NO-OP. An overlay whose agents never emit one keeps
    exactly today's behaviour — no new refusal — and its fix-tickets still reach
    DELIVERED through the audited override.
* a record that is ONLY an ``already_fixed`` claim is the no-commit ending: it needs no DoD fields,
    is not stored, and is verified against the worktree HEAD by ``landing_verification``.
* a PRESENT-but-malformed one is REFUSED by name, in the same shape as the other
    envelope refusals, so a dropped record can never read as an absent one. The
    reason is prefixed with the shared :data:`MALFORMED_FIX_RECORD_PREFIX`, which
    ``envelope_refusal.is_recorder_refusal`` classifies — so the malformed case earns
    ``loop.transient_requeue``'s one-shot corrective retry on coding/debugging (the
    fixing phases) instead of paging a human.

Split out of ``attempt_recorder`` rather than inlined: that module sits within a
handful of lines of its module-health LOC cap, the same reason
``reactive_envelope_recorders`` lives beside it.
"""

from typing import cast

from teatree.agents.envelope_refusal import MALFORMED_FIX_RECORD_PREFIX
from teatree.agents.result_schema import AgentResultBlob
from teatree.core.gates.repro_gate import failing_run_required
from teatree.core.modelkit.phase_tools import ENVELOPE_VERDICT_PHASES
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import Task, Ticket
from teatree.core.models.fix_record_types import (
    FixRecord,
    already_fixed_claim,
    fix_record_missing_fields,
    fix_record_required_fields,
    is_claim_only,
)

_BLANK_CLAIM_REFUSAL = f"{MALFORMED_FIX_RECORD_PREFIX}already_fixed needs both a verified_head and non-blank evidence."


def record_returned_fix_record(task: Task, result: AgentResultBlob) -> str:
    """Persist *result*'s ``fix_record``; return a refusal reason, or ``""`` when there is none.

    Kind-agnostic on purpose: the gate decides what to READ (only ``kind=fix``), so a
    record emitted on a feature ticket is recorded rather than dropped — correct if
    the ticket is later re-classified — and a malformed one is refused either way.
    A verdict-review phase is left to ``review_envelope_recorder``, which writes it on the
    gated ticket with a merge_safe verdict; its ``task.ticket`` is the reviewer row.
    """
    raw = result.get("fix_record")
    if raw is None or normalize_phase(task.phase) in ENVELOPE_VERDICT_PHASES:
        return ""
    if is_claim_only(raw):
        return "" if all(already_fixed_claim(raw)) else _BLANK_CLAIM_REFUSAL
    if refusal := fix_record_refusal(raw, ticket=task.ticket):
        return refusal
    task.ticket.record_fix_record(cast("FixRecord", raw))
    return ""


def fix_record_refusal(raw: object, *, ticket: Ticket | None = None) -> str:
    """The refusal for a present but incomplete ``fix_record``, or ``""`` when it is complete."""
    red_required = ticket is None or failing_run_required(ticket)
    missing = fix_record_missing_fields(raw, red_required=red_required)
    if not missing:
        return ""
    return (
        f"{MALFORMED_FIX_RECORD_PREFIX}these required field(s) are absent or blank: "
        f"{', '.join(missing)}. The fix-record DoD gate consumes all of "
        f"{', '.join(fix_record_required_fields(red_required=red_required))}, so a partial record satisfies nothing — "
        f"return the whole object or omit the key entirely."
    )
