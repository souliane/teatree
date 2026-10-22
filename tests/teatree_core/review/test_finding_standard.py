from teatree.core.review.finding_standard import FindingKind, normalize_finding_kind


def test_explicit_kind_takes_precedence_over_legacy_severity() -> None:
    assert normalize_finding_kind("nit", "major") == FindingKind.NIT
    assert normalize_finding_kind("blocking", "nit") == FindingKind.BLOCKING


def test_unknown_kind_fails_closed_and_legacy_nit_stays_readable() -> None:
    assert normalize_finding_kind("unknown", "nit") == FindingKind.BLOCKING
    assert normalize_finding_kind("", "nit") == FindingKind.NIT
    assert normalize_finding_kind("", "minor") == FindingKind.BLOCKING
