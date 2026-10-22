from teatree.core.models.finding_standard import FindingKind, normalize_finding_kind


def test_the_model_rule_normalizes_legacy_and_explicit_findings() -> None:
    assert normalize_finding_kind("nit", "major") == FindingKind.NIT
    assert normalize_finding_kind("", "nit") == FindingKind.NIT
    assert normalize_finding_kind("unknown", "nit") == FindingKind.BLOCKING
