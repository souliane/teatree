"""The shared blocking versus NIT standard for review and merge criticism."""

from enum import StrEnum


class FindingKind(StrEnum):
    BLOCKING = "blocking"
    NIT = "nit"


FINDING_STANDARD = (
    "BLOCKING: wrong behaviour, a false statement about current behaviour, shipped behaviour no test pins, "
    "a vacuous test, a test shown redundant by mutation, a duplicated definition introduced by the diff, "
    "or a red required check. NIT: wording, naming, comment placement, ordering, missing docstrings, or style. "
    "Only BLOCKING findings may fail a critic item or hold a review."
)


def normalize_finding_kind(kind: str, severity: str) -> str:
    normalized = kind.strip().lower()
    if normalized == FindingKind.NIT:
        return FindingKind.NIT
    if normalized:
        return FindingKind.BLOCKING
    return FindingKind.NIT if severity.strip().lower() == "nit" else FindingKind.BLOCKING
