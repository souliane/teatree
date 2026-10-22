from teatree.core.models.finding_standard import FindingKind, normalize_finding_kind


def test_a_blocking_severity_overrides_a_nit_label() -> None:
    assert normalize_finding_kind("nit", "major") == FindingKind.BLOCKING
    assert normalize_finding_kind("blocking", "nit") == FindingKind.BLOCKING


def test_unknown_or_missing_kind_fails_closed() -> None:
    assert normalize_finding_kind("unknown", "nit") == FindingKind.BLOCKING
    assert normalize_finding_kind("", "nit") == FindingKind.BLOCKING
    assert normalize_finding_kind("", "minor") == FindingKind.BLOCKING
    assert normalize_finding_kind("nit", "minor") == FindingKind.NIT
