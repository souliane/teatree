from teatree.core.models.finding_standard import FindingKind, normalize_finding_kind
from teatree.core.models.review_finding import Finding


def test_a_blocking_severity_overrides_a_nit_label() -> None:
    assert normalize_finding_kind("nit", "major") == FindingKind.BLOCKING
    assert normalize_finding_kind("blocking", "nit") == FindingKind.BLOCKING


def test_unknown_or_missing_kind_fails_closed() -> None:
    assert normalize_finding_kind("unknown", "nit") == FindingKind.BLOCKING
    assert normalize_finding_kind("", "nit") == FindingKind.BLOCKING
    assert normalize_finding_kind("", "minor") == FindingKind.BLOCKING
    assert normalize_finding_kind("nit", "minor") == FindingKind.NIT


def test_explicit_note_cannot_hide_a_reproduced_blocker() -> None:
    finding = Finding(
        severity="blocker", kind="note", basis="executed", reproduction="pytest fails: 1 failed", summary="failure"
    )
    assert finding.kind == FindingKind.BLOCKING
