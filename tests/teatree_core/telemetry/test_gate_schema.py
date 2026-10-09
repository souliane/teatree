"""The gate-ledger reader accepts only bounded, well-formed rows."""

import pytest

from teatree.core.telemetry.gate_schema import valid_gate_row

_NOW = 1_790_000_000
_ROW = {
    "epoch": _NOW,
    "trace_id": "1" * 32,
    "span_id": "2" * 16,
    "gate": "plan_gate",
    "decision": "deny",
    "rule": "blocked",
    "session_ref": "a" * 16,
}


def _valid(row: object) -> bool:
    return valid_gate_row(row, cutoff=_NOW - 60, current=_NOW)


def test_a_well_formed_row_is_accepted_with_or_without_a_session_ref() -> None:
    assert _valid(_ROW)
    assert _valid({key: value for key, value in _ROW.items() if key != "session_ref"})


@pytest.mark.parametrize(
    "change",
    [
        {"trace_id": "0" * 32},
        {"span_id": "0" * 16},
        {"trace_id": "A" * 32},
        {"epoch": True},
        {"epoch": _NOW + 1},
        {"epoch": _NOW - 61},
        {"gate": "Plan Gate"},
        {"rule": "x" * 65},
        {"decision": "allow"},
        {"session_ref": "not-a-session-ref"},
        {"extra": 1},
    ],
    ids=[
        "zero-trace",
        "zero-span",
        "uppercase-trace",
        "bool-epoch",
        "future-epoch",
        "expired-epoch",
        "unsafe-gate",
        "long-rule",
        "unknown-decision",
        "bad-session-ref",
        "extra-field",
    ],
)
def test_a_malformed_row_is_rejected(change: dict) -> None:
    assert not _valid(_ROW | change)


def test_a_non_mapping_or_a_row_missing_a_field_is_rejected() -> None:
    assert not _valid([_ROW])
    assert not _valid({key: value for key, value in _ROW.items() if key != "rule"})
