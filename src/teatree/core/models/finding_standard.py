"""The shared blocking versus NIT standard for review and merge criticism."""

from enum import StrEnum

from teatree.core.modelkit.diff_scope import BLOCKING_SEVERITIES


class FindingKind(StrEnum):
    BLOCKING = "blocking"
    NIT = "nit"
    NOTE = "note"


FINDING_STANDARD = (
    "BLOCKING: wrong behaviour, a false statement about current behaviour, shipped behaviour no test pins, "
    "a vacuous test, a test shown redundant by mutation, a duplicated definition introduced by the diff, "
    "or a red required check. NIT: wording, naming, comment placement, ordering, missing docstrings, or style. "
    "NOTE: a venue cannot read or run a check, or documentation suggests a defect without executed reproduction. "
    "Only BLOCKING findings may fail a critic item or hold a review."
)


def normalize_finding_kind(kind: str, severity: str) -> str:
    normalized = kind.strip().lower()
    if severity.strip().lower() in BLOCKING_SEVERITIES:
        return FindingKind.BLOCKING
    if normalized == FindingKind.NIT:
        return FindingKind.NIT
    return FindingKind.BLOCKING
