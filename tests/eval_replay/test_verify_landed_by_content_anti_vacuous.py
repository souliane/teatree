"""Anti-vacuity proof for the ``verify_landed_by_content_not_sha_ancestry`` scenario.

The scenario pins the dream-ledger rule: on a squash-merging repo a change is
verified by reading its CONTENT on the target, never by a sha-ancestry probe
(``skills/rules/SKILL.md`` § "Verification Before Completion").

Its two teeth are:

*   a positive ANCHOR — a ``tool_call`` requiring a content read, so a no-op turn
    (reasoning about the exit code, running nothing) grades RED; and
*   the DISCRIMINATING negative tooth — a ``no_tool_call_matching`` forbidding any
    sha / ancestry / range probe.

The ``_fail`` fixture is the belt-and-braces agent: it runs the forbidden range
probe AND a content read. That shape satisfies the anchor, so the negative tooth is
the ONLY matcher rejecting it — which is what proves that tooth load-bearing. A
``_fail`` that ran the probe alone would be rejected by the anchor as well, and
dropping the negative tooth would then change nothing.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import ScenarioResult, evaluate

_SCENARIO = "verify_landed_by_content_not_sha_ancestry"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"
_FAIL_FIXTURE = _FIXTURES / f"{_SCENARIO}_fail.stream.jsonl"
_PASS_FIXTURE = _FIXTURES / f"{_SCENARIO}_pass.stream.jsonl"
_NOOP_FIXTURE = _FIXTURES / f"{_SCENARIO}_noop.stream.jsonl"


def _scenario_spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, f"scenario {_SCENARIO!r} not discovered — check evals/scenarios/rules.yaml"
    return spec


def _without_matcher(spec: EvalSpec, index: int) -> EvalSpec:
    return dataclasses.replace(spec, matchers=tuple(m for i, m in enumerate(spec.matchers) if i != index))


def _result(spec: EvalSpec, fixture_path: Path, tmp_path: Path) -> ScenarioResult:
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture_path.read_text(encoding="utf-8"), encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run)


def test_pass_fixture_drives_scenario_green(tmp_path: Path) -> None:
    result = _result(_scenario_spec(), _PASS_FIXTURE, tmp_path)
    assert result.passed is True, "the content-read _pass fixture must grade GREEN"


def test_noop_fixture_is_caught_by_the_anchor_alone(tmp_path: Path) -> None:
    spec = _scenario_spec()
    assert _result(spec, _NOOP_FIXTURE, tmp_path).passed is False
    assert _result(_without_matcher(spec, 0), _NOOP_FIXTURE, tmp_path).passed is True, (
        "with the positive anchor removed the _noop fixture must go GREEN — the anchor is what catches it"
    )


def test_fail_fixture_drives_scenario_red(tmp_path: Path) -> None:
    result = _result(_scenario_spec(), _FAIL_FIXTURE, tmp_path)
    assert result.passed is False, "the sha-probe _fail fixture must grade RED"
    assert result.run.terminal_reason == "success", "the _fail fixture must terminate cleanly, not hit the cap"
    assert not result.run.is_error


def test_fail_fixture_is_rejected_by_the_negative_tooth_alone(tmp_path: Path) -> None:
    # The proof the tooth is load-bearing: it must be the ONLY matcher the _fail
    # fixture fails, else dropping it changes nothing and the tooth guards nothing.
    spec = _scenario_spec()
    failing = [i for i, m in enumerate(_result(spec, _FAIL_FIXTURE, tmp_path).matcher_results) if not m.passed]
    assert failing == [1], (
        f"the _fail fixture must fail ONLY the no_tool_call_matching tooth, failed matchers: {failing}"
    )
    assert _result(_without_matcher(spec, 1), _FAIL_FIXTURE, tmp_path).passed is True, (
        "with the negative tooth removed the _fail fixture must go GREEN"
    )
    assert _result(_without_matcher(spec, 0), _FAIL_FIXTURE, tmp_path).passed is False, (
        "with only the anchor removed the _fail fixture must stay RED — the tooth, not the anchor, rejects it"
    )
