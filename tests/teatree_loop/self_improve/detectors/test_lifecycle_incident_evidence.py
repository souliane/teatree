"""Evidence formatting for attempt bursts."""

from teatree.loop.self_improve.detectors.lifecycle_incident_evidence import AttemptBurstEvidence


def test_distinct_failures_filter_duplicates_and_missing_errors() -> None:
    rows = [
        {"epoch": 1, "entity_id": 1, "task_id": 10, "cause": "result_schema_invalid"},
        {"epoch": 2, "entity_id": 1, "task_id": 10, "cause": "result_schema_invalid"},
        {"epoch": 3, "entity_id": 2, "task_id": 10, "cause": "other"},
        {"epoch": 4, "entity_id": 3, "task_id": 30, "cause": "result_schema_invalid"},
        {"epoch": 5, "entity_id": 4, "task_id": 40, "cause": "result_schema_invalid"},
    ]

    digest = AttemptBurstEvidence.distinct_failures(
        rows, "result_schema_invalid", {10, 40}, {1: "first line\nsecond line"}
    )

    assert digest == "attempt 1 (task 10): first line; attempt 4 (task 40)"


def test_distinct_failures_omits_causeless_attempts() -> None:
    rows = [{"epoch": 1, "entity_id": 1, "task_id": 10, "cause": "no_result_envelope"}]

    assert AttemptBurstEvidence.distinct_failures(rows, "no_result_envelope", {10}, {1: "missing result"}) == ""


def test_recurring_fingerprint_rejects_incomplete_and_unrelated_rows() -> None:
    rows = [
        {
            "epoch": 1,
            "entity_id": 1,
            "task_id": 10,
            "cause": "result_schema_invalid",
            "error_fingerprint": "a" * 64,
            "trace_id": "1" * 32,
            "span_id": "1" * 16,
        },
        {
            "epoch": 2,
            "entity_id": 2,
            "task_id": 10,
            "cause": "result_schema_invalid",
            "error_fingerprint": "a" * 64,
            "trace_id": "2" * 32,
        },
        {
            "epoch": 3,
            "entity_id": 3,
            "task_id": 20,
            "cause": "other",
            "error_fingerprint": "a" * 64,
            "trace_id": "3" * 32,
            "span_id": "3" * 16,
        },
    ]

    assert AttemptBurstEvidence.recurring_fingerprint(rows, "result_schema_invalid", {10}) is None
