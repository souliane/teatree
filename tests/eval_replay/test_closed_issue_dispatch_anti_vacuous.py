"""Anti-vacuity proof for the paired closed-issue dispatch scenarios.

The pair pins the #2663 dream-gap rule at the AGENT level, beside the code gate
(``core/gates/closed_issue_dispatch_gate``): an implementing brief whose issue the
owner already closed NOT_PLANNED must stop and ask, while the same brief on a LIVE
issue must proceed. Two scenarios rather than one because the over-correction is a
real failure too — a gate that taught the agent to stall on every issue-state read
would trade one stuck loop for another.

Each scenario has the same two teeth:

*   a positive ANCHOR, so a turn that only narrates the situation grades RED; and
*   a DISCRIMINATING negative tooth — the implement-anyway commands on the closed
    side, the invented-closure question on the open side.

Both ``_fail`` fixtures are deliberately belt-and-braces: they satisfy the anchor
and are rejected by the negative tooth ALONE, which is what proves that tooth
load-bearing rather than redundant with the anchor.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

import pytest

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import ScenarioResult, evaluate

_CLOSED = "closed_issue_dispatch_stops_and_asks"
_OPEN = "open_issue_dispatch_implements_without_asking"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"


def _spec(name: str) -> EvalSpec:
    spec = find_spec(name)
    assert spec is not None, f"scenario {name!r} not discovered — check evals/scenarios/rules.yaml"
    return spec


def _without_matcher(spec: EvalSpec, index: int) -> EvalSpec:
    return dataclasses.replace(spec, matchers=tuple(m for i, m in enumerate(spec.matchers) if i != index))


def _result(spec: EvalSpec, fixture: Path, tmp_path: Path) -> ScenarioResult:
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    return evaluate(spec, TranscriptRunner(transcript_dir=tmp_path).run(spec))


def _fixture(name: str, kind: str) -> Path:
    return _FIXTURES / f"{name}_{kind}.stream.jsonl"


@pytest.mark.parametrize("scenario", [_CLOSED, _OPEN])
def test_pass_fixture_drives_scenario_green(scenario: str, tmp_path: Path) -> None:
    assert _result(_spec(scenario), _fixture(scenario, "pass"), tmp_path).passed is True


@pytest.mark.parametrize("scenario", [_CLOSED, _OPEN])
def test_noop_fixture_is_caught_by_the_anchor_alone(scenario: str, tmp_path: Path) -> None:
    spec = _spec(scenario)
    assert _result(spec, _fixture(scenario, "noop"), tmp_path).passed is False
    assert _result(_without_matcher(spec, 0), _fixture(scenario, "noop"), tmp_path).passed is True, (
        "with the positive anchor removed the _noop fixture must go GREEN — the anchor is what catches it"
    )


@pytest.mark.parametrize("scenario", [_CLOSED, _OPEN])
def test_fail_fixture_drives_scenario_red(scenario: str, tmp_path: Path) -> None:
    result = _result(_spec(scenario), _fixture(scenario, "fail"), tmp_path)
    assert result.passed is False
    assert result.run.terminal_reason == "success", "the _fail fixture must terminate cleanly, not hit the cap"
    assert not result.run.is_error


@pytest.mark.parametrize("scenario", [_CLOSED, _OPEN])
def test_fail_fixture_is_rejected_by_the_negative_tooth_alone(scenario: str, tmp_path: Path) -> None:
    # The proof the tooth is load-bearing: it must be the ONLY matcher the _fail
    # fixture fails, else dropping it changes nothing and the tooth guards nothing.
    spec = _spec(scenario)
    fixture = _fixture(scenario, "fail")
    failing = [i for i, m in enumerate(_result(spec, fixture, tmp_path).matcher_results) if not m.passed]
    assert failing == [1], f"the _fail fixture must fail ONLY the negative tooth, failed matchers: {failing}"
    assert _result(_without_matcher(spec, 1), fixture, tmp_path).passed is True, (
        "with the negative tooth removed the _fail fixture must go GREEN"
    )
    assert _result(_without_matcher(spec, 0), fixture, tmp_path).passed is False, (
        "with only the anchor removed the _fail fixture must stay RED — the tooth, not the anchor, rejects it"
    )
