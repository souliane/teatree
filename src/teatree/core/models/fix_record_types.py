"""The fix-ticket Definition-of-Done record and its field roster, carried on ``Ticket.extra['fix_record']``."""

from collections.abc import Mapping
from typing import Final, TypedDict


class AlreadyFixed(TypedDict):
    verified_head: str
    evidence: str


class FixRecord(TypedDict, total=False):
    """A fix-ticket's Definition-of-Done record (#1661), carried on ``Ticket.extra['fix_record']``.

    Produced by the fixing agent in its result envelope, validated and written by
    ``teatree.agents.fix_record_recorder``, read by ``core.gates.fix_dod_gate`` at
    ``mark_delivered``. Every field is required and non-blank when forced repro is on;
    ``observed_red`` is optional when it is off — a manifestation patch
    with no stated root cause is exactly the case the gate exists to catch, so there
    is no partial credit.

    Declared here rather than beside the gate because this declaration is
    load-bearing twice over: it is what ``validated_ticket_extra`` keeps (an
    undeclared key is stripped by every ``_extra()`` write-back, so a record written
    at the coding phase would not survive ``Ticket.test()``), and
    :data:`FIX_RECORD_FIELDS` derived from it is the ONE definition the gate, the
    recorder, the envelope schema and the agent brief all read. ``already_fixed`` is the one
    optional claim: a no-commit ending that names the HEAD it verified and the evidence, read
    by ``agents.already_fixed``, accepted alone as a record and never stored or required.
    """

    root_cause: str
    evidence: str
    regression_test: str
    observed_red: str
    recurrence_fingerprint: str
    already_fixed: AlreadyFixed


class FixRecordOverride(TypedDict, total=False):
    """Audited escape hatch for a fix-ticket the FixRecord heuristic mis-classifies (#1661).

    ``Ticket.extra['fix_record_override']`` with a non-empty ``reason`` makes the
    gate pass-and-log. Deliberately weaker than the forced-repro gate's
    human-authorized ``ReproWaiver`` — this one is self-authored, which is why it is
    the exception rather than the route (the envelope is the route).
    """

    reason: str


#: The FixRecord fields, in declaration order — THE definition. The gate's required
#: set, the envelope JSON schema, the recorder's validator and the agent brief all
#: derive from this tuple, so a field added to :class:`FixRecord` reaches every one
#: of them and none can drift into a second hand-typed list.
FIX_RECORD_FIELDS: Final[tuple[str, ...]] = tuple(
    field for field in FixRecord.__annotations__ if field != "already_fixed"
)


def fix_record_required_fields(*, red_required: bool = True) -> tuple[str, ...]:
    return FIX_RECORD_FIELDS if red_required else tuple(field for field in FIX_RECORD_FIELDS if field != "observed_red")


def fix_record_missing_fields(record: object, *, red_required: bool = True) -> list[str]:
    """The required fields *record* leaves absent or blank, in declaration order.

    The one parse shared by the gate that READS the stored record and the recorder
    that VALIDATES the returned one, so producer and consumer cannot drift. A
    non-mapping (or absent) record yields every required field — there is no partial credit.
    """
    required = fix_record_required_fields(red_required=red_required)
    if not isinstance(record, dict):
        return list(required)
    return [field for field in required if not str(record.get(field, "")).strip()]


def already_fixed_claim(record: object) -> tuple[str, str]:
    """The ``(verified_head, evidence)`` *record*'s ``already_fixed`` names, blank when absent."""
    claim = record.get("already_fixed") if isinstance(record, Mapping) else None
    if not isinstance(claim, Mapping):
        return "", ""
    return str(claim.get("verified_head") or "").strip().lower(), str(claim.get("evidence") or "").strip()


def is_claim_only(record: object) -> bool:
    """Whether *record* carries ``already_fixed`` and none of the DoD fields, so it is a no-commit ending alone."""
    return (
        isinstance(record, Mapping)
        and "already_fixed" in record
        and not any(str(record.get(field, "")).strip() for field in FIX_RECORD_FIELDS)
    )
