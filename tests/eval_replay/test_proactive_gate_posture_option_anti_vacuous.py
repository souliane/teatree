r"""The ``preset\s+use`` alternation is load-bearing, and nothing pinned it (D3).

The sweep added that alternation because an agent offering only the doctrine's new
option 1 — select a permitting posture — scored FAIL under the old regex. But the
shipped ``_pass`` fixture also says "approve-on-behalf", which the regex matched
BEFORE the alternation existed, so deleting it again would break no test.

The fixture here says ONLY ``t3 loop preset use present``. It grades GREEN against the
live scenario and RED with that one alternation removed, which is what makes the
alternation's removal a test failure rather than a silent regression.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec, Matcher
from teatree.eval.report import ScenarioResult, evaluate

_SCENARIO = "proactive_gate_offers_enable_or_approve_once"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"
_POSTURE_ONLY_FIXTURE = _FIXTURES / f"{_SCENARIO}_pass_posture_only.stream.jsonl"
_SHIPPED_PASS_FIXTURE = _FIXTURES / f"{_SCENARIO}_pass.stream.jsonl"
_POSTURE_NAMED_FIXTURE = _FIXTURES / f"{_SCENARIO}_pass_posture_named.stream.jsonl"

_ALTERNATION = r"preset\s+use|"
#: The posture may be NAMED instead of spelled as the command; this carries that half.
_POSTURE_NAME_ALTERNATION = r"|present\s+(mode|posture|preset)|(mode|posture|preset)\s+to\s+\W{0,2}present"


def _scenario_spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, f"scenario {_SCENARIO!r} not discovered — check evals/scenarios/"
    return spec


def _result(spec: EvalSpec, fixture_path: Path, tmp_path: Path) -> ScenarioResult:
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture_path.read_text(encoding="utf-8"), encoding="utf-8")
    return evaluate(spec, TranscriptRunner(transcript_dir=tmp_path).run(spec))


def _mutated(spec: EvalSpec, alternation: str) -> EvalSpec:
    """*spec* with *alternation* struck from every positive matcher — the driven mutation."""
    matchers = tuple(
        dataclasses.replace(m, value=m.value.replace(alternation, ""))
        if isinstance(m, Matcher) and m.kind == "positive"
        else m
        for m in spec.matchers
    )
    return dataclasses.replace(spec, matchers=matchers)


def test_the_live_scenario_carries_the_posture_alternation() -> None:
    positives = [m for m in _scenario_spec().matchers if isinstance(m, Matcher) and m.kind == "positive"]
    assert positives, "the scenario has no positive matcher to carry the alternation"
    assert all(_ALTERNATION in m.value for m in positives)


def test_a_posture_only_offer_grades_green(tmp_path: Path) -> None:
    result = _result(_scenario_spec(), _POSTURE_ONLY_FIXTURE, tmp_path)
    assert result.passed is True, (
        "an offer naming only `t3 loop preset use present` must grade GREEN — "
        "the doctrine's option 1 is a compliant answer"
    )


def test_removing_the_posture_alternation_turns_it_red(tmp_path: Path) -> None:
    result = _result(_mutated(_scenario_spec(), _ALTERNATION), _POSTURE_ONLY_FIXTURE, tmp_path)
    assert result.passed is False, (
        "with `preset\\s+use` removed the posture-only fixture must grade RED — "
        "otherwise the alternation is unpinned and can be deleted silently"
    )


def test_naming_the_posture_grades_green(tmp_path: Path) -> None:
    """Naming the posture is the same durable option as spelling out the command."""
    result = _result(_scenario_spec(), _POSTURE_NAMED_FIXTURE, tmp_path)
    assert result.passed is True


def test_removing_the_posture_name_alternation_turns_it_red(tmp_path: Path) -> None:
    spec = _mutated(_scenario_spec(), _POSTURE_NAME_ALTERNATION)
    assert _result(spec, _POSTURE_NAMED_FIXTURE, tmp_path).passed is False


def test_the_shipped_pass_fixture_could_not_have_been_the_pin(tmp_path: Path) -> None:
    r"""Why the fixture above had to be added: the existing one matches either way.

    It offers `approve-on-behalf` alongside the posture, and that alternation predates
    the sweep — so it grades GREEN with `preset\s+use` deleted. Reading it as coverage
    is the mistake this asserts against.
    """
    result = _result(_mutated(_scenario_spec(), _ALTERNATION), _SHIPPED_PASS_FIXTURE, tmp_path)
    assert result.passed is True
