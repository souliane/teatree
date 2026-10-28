"""The full-suite baseline report: MISSING, STALE, NOT_GREEN or GREEN (#4933).

A recurring one-shard smoke run can stay green without saying anything about the rest of
the catalog. The baseline is therefore judged from the newest merged ``eval-heal`` summary
on its own: it must exist, carry rows, cover the CURRENT catalog's exact
identity set, must be recent, and must carry no gating red.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from teatree.eval.green_proof import BASELINE_MAX_AGE, CLOCK_SKEW_TOLERANCE, BaselineState, evaluate_baseline
from teatree.eval.parallel import usage_limit_skip
from teatree.eval.summary_json_merge import merge_summary_payloads

_NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
_SHA = "0123456789abcdef0123456789abcdef01234567"
_EXPECTED = {"a": "va", "b": "vb"}


def _row(name: str, version: str, *, red: bool = False) -> dict[str, object]:
    return {
        "name": name,
        "version": version,
        "lane": "clean_room",
        "verdict": "fail" if red else "pass",
        "outcome": "BEHAVIOR_FAIL" if red else "PASS",
        "triage_class": "behavioral" if red else None,
    }


def _payload(*, age: timedelta = timedelta(days=1), red: bool = False, **overrides: Any) -> dict[str, Any]:
    rows = [_row("a", "va", red=red), _row("b", "vb")]
    payload: dict[str, Any] = {
        "head_sha": _SHA,
        "generated_at": (_NOW - age).isoformat().replace("+00:00", "Z"),
        "totals": {"total": 2, "passed": 1 if red else 2, "failed": 1 if red else 0, "skipped": 0, "incomplete": 0},
        "scenarios": rows,
    }
    payload.update(overrides)
    return payload


def _state(*payloads: dict[str, Any], expected: dict[str, str] | None = None) -> BaselineState:
    return evaluate_baseline(list(payloads), expected=expected or _EXPECTED, now=_NOW).state


def test_a_recent_green_proof_over_the_whole_catalog_is_green() -> None:
    assert _state(_payload()) is BaselineState.GREEN


def test_no_run_on_record_is_missing() -> None:
    status = evaluate_baseline([], expected=_EXPECTED, now=_NOW)

    assert status.state is BaselineState.MISSING
    assert "no full-suite eval-heal run" in status.summary


def test_a_parked_run_measured_nothing_so_it_is_missing() -> None:
    assert _state(_payload(parked=True, scenarios=[], totals={"total": 0})) is BaselineState.MISSING


def test_the_newest_run_with_rows_is_the_one_judged() -> None:
    assert (
        _state(_payload(parked=True, scenarios=[], totals={"total": 0}), _payload(age=timedelta(days=2)))
        is BaselineState.GREEN
    )


def test_a_parked_merge_with_gating_red_cannot_hide_behind_an_older_green() -> None:
    red_shard = _payload(
        scenarios=[_row("a", "va", red=True)],
        totals={"total": 1, "passed": 0, "failed": 1, "skipped": 0},
    )
    green_shard = _payload(
        scenarios=[_row("b", "vb")],
        totals={"total": 1, "passed": 1, "failed": 0, "skipped": 0},
    )
    merged = merge_summary_payloads(
        [red_shard, green_shard, {"head_sha": _SHA, "parked": True}],
        head_sha=_SHA,
        generated_at=red_shard["generated_at"],
    )

    status = evaluate_baseline([merged, _payload(age=timedelta(days=5))], expected=_EXPECTED, now=_NOW)

    assert merged["parked"] is True
    assert status.state is BaselineState.NOT_GREEN
    assert "1 gating red" in status.detail


def test_a_newer_partial_run_does_not_hide_behind_an_older_full_one() -> None:
    partial = _payload(scenarios=[_row("a", "va")], totals={"total": 1, "passed": 1, "failed": 0, "skipped": 0})

    status = evaluate_baseline([partial, _payload(age=timedelta(days=2))], expected=_EXPECTED, now=_NOW)

    assert status.state is BaselineState.STALE
    assert "1 of 2" in status.detail


def test_a_proof_older_than_the_window_is_stale() -> None:
    status = evaluate_baseline([_payload(age=BASELINE_MAX_AGE + timedelta(days=1))], expected=_EXPECTED, now=_NOW)

    assert status.state is BaselineState.STALE
    assert "8 day(s) old" in status.detail


def test_a_proof_exactly_at_the_window_edge_is_still_current() -> None:
    assert _state(_payload(age=BASELINE_MAX_AGE)) is BaselineState.GREEN


def test_a_proof_whose_catalog_has_since_changed_is_stale() -> None:
    assert _state(_payload(), expected={"a": "va", "b": "edited"}) is BaselineState.STALE


def test_a_scenario_added_since_the_proof_is_stale() -> None:
    assert _state(_payload(), expected={**_EXPECTED, "c": "vc"}) is BaselineState.STALE


@pytest.mark.parametrize("generated_at", [None, "", "yesterday", 7])
def test_a_proof_without_a_readable_timestamp_is_stale(generated_at: object) -> None:
    assert _state(_payload(generated_at=generated_at)) is BaselineState.STALE


def test_a_timestamp_without_an_offset_is_read_as_utc() -> None:
    assert _state(_payload(generated_at="2026-10-26T12:00:00")) is BaselineState.GREEN


def test_a_current_proof_with_a_gating_red_is_not_green() -> None:
    status = evaluate_baseline([_payload(red=True)], expected=_EXPECTED, now=_NOW)

    assert status.state is BaselineState.NOT_GREEN
    assert "1 gating red" in status.detail


def test_the_window_is_a_parameter() -> None:
    status = evaluate_baseline(
        [_payload(age=timedelta(days=3))], expected=_EXPECTED, now=_NOW, max_age=timedelta(days=2)
    )

    assert status.state is BaselineState.STALE


def test_only_a_green_state_counts_as_green() -> None:
    green = evaluate_baseline([_payload()], expected=_EXPECTED, now=_NOW)
    missing = evaluate_baseline([], expected=_EXPECTED, now=_NOW)

    assert (green.is_green, missing.is_green) == (True, False)


@pytest.mark.parametrize(
    ("ahead", "named"),
    [
        (timedelta(minutes=10), "10 minute(s) in the future"),
        (timedelta(hours=3), "3 hour(s) in the future"),
        (timedelta(days=1, hours=5), "1 day(s) in the future"),
    ],
    ids=["minutes", "hours", "days"],
)
def test_a_future_stamp_beyond_the_skew_allowance_names_its_real_offset(ahead: timedelta, named: str) -> None:
    status = evaluate_baseline([_payload(age=-ahead)], expected=_EXPECTED, now=_NOW)

    assert status.state is BaselineState.STALE
    assert named in status.detail


@pytest.mark.parametrize("ahead", [timedelta(seconds=30), CLOCK_SKEW_TOLERANCE])
def test_a_stamp_a_few_minutes_ahead_is_clock_skew_not_stale(ahead: timedelta) -> None:
    status = evaluate_baseline([_payload(age=-ahead)], expected=_EXPECTED, now=_NOW)

    assert status.state is BaselineState.GREEN
    assert status.detail.endswith("0 day(s) old")


def test_a_stamp_just_past_the_skew_allowance_is_stale() -> None:
    ahead = CLOCK_SKEW_TOLERANCE + timedelta(seconds=1)

    assert _state(_payload(age=-ahead)) is BaselineState.STALE


def _skipped(name: str, version: str) -> dict[str, object]:
    return {
        "name": name,
        "version": version,
        "lane": "clean_room",
        "verdict": "skip",
        "outcome": "UNVERIFIED",
        "triage_class": "no_coverage",
        "terminal_reason": usage_limit_skip(name).terminal_reason,
    }


def test_a_proof_the_usage_limit_cut_short_names_its_unmeasured_rows() -> None:
    rows = [_row("a", "va"), _skipped("b", "vb")]
    totals = {"total": 2, "passed": 1, "failed": 0, "skipped": 1, "incomplete": 0}

    status = evaluate_baseline([_payload(scenarios=rows, totals=totals)], expected=_EXPECTED, now=_NOW)

    assert status.state is BaselineState.NOT_GREEN
    assert "unmeasured: b" in status.detail


def test_an_all_skipped_proof_over_a_changed_catalog_names_its_unmeasured_rows() -> None:
    rows = [_skipped("a", "va"), _skipped("b", "vb")]
    totals = {"total": 2, "passed": 0, "failed": 0, "skipped": 2, "incomplete": 0}

    status = evaluate_baseline([_payload(scenarios=rows, totals=totals)], expected={**_EXPECTED, "c": "vc"}, now=_NOW)

    assert status.state is BaselineState.STALE
    assert "unmeasured: a, b" in status.detail
