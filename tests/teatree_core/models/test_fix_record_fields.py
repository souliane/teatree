from teatree.core.models.types import fix_record_required_fields


def test_forced_repro_requires_every_fix_record_field() -> None:
    assert fix_record_required_fields(red_required=True) == (
        "root_cause",
        "evidence",
        "regression_test",
        "observed_red",
        "recurrence_fingerprint",
    )


def test_opted_out_repro_only_relaxes_red_attestation() -> None:
    assert fix_record_required_fields(red_required=False) == (
        "root_cause",
        "evidence",
        "regression_test",
        "recurrence_fingerprint",
    )
