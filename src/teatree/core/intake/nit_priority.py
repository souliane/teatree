"""Recognize NIT follow-up issues at intake and preserve their ticket priority."""

from teatree.core.intake.factory_admission import payload_labels
from teatree.types import RawAPIDict


def is_nit_issue(issue: RawAPIDict) -> bool:
    return "nit" in {label.casefold() for label in payload_labels(issue)}


def nit_ticket_extra(extra: object, issue: RawAPIDict) -> RawAPIDict:
    current = dict(extra) if isinstance(extra, dict) else {}
    if is_nit_issue(issue):
        current["nit_follow_up"] = True
    return current
