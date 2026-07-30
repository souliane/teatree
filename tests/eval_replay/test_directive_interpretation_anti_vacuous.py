"""Anti-vacuity + contract-drift + exemplar-conformance for the PR-8 directive evals.

Three free deterministic trip-wires that red on every PR:

*   **anti-vacuity** — for BOTH scenarios the ``_fail`` fixture grades RED, the
    ``_pass`` GREEN, and the ``_noop`` (a do-nothing transcript) cannot satisfy; and
    removing the matchers turns the ``_fail`` fixture GREEN (the mandatory teeth check
    — a matcher whose removal does not change the verdict guards nothing);
*   **contract-drift** — the interpretation scenario's YAML prompt is pinned equal to
    ``build_interpreter_contract(...)``, so a doctrine edit forces the scenario to
    follow rather than silently drift;
*   **exemplar-conformance** — each interpretation matcher regex matches the serialized
    dogfood ``EXEMPLAR_ENVELOPE``, so the dogfood fixture and the eval's expected answer
    can never diverge.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by the
# established eval-suite convention.

import dataclasses
import json
import re
from pathlib import Path

import pytest

from teatree.core.models import Directive
from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec, FinalStateMatcher
from teatree.eval.report import evaluate
from teatree.loops.directive_loop.interpret import build_interpreter_contract
from tests.integration.directive_dogfood.exemplar import EXEMPLAR_ENVELOPE, PROOF_CASE_TEXT, SCOPE

_INTERP = "directive_interpreter_finds_existing_mechanism_activation_only"
_CONFORM = "directive_activation_conforms_to_ratified_sketch_under_load"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"


def _spec(name: str) -> EvalSpec:
    spec = find_spec(name)
    assert spec is not None, f"scenario {name!r} not discovered — check evals/scenarios/directive_interpretation.yaml"
    return spec


def _grade(spec: EvalSpec, suffix: str, tmp_path: Path) -> bool:
    fixture = _FIXTURES / f"{spec.name}_{suffix}.stream.jsonl"
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


@pytest.mark.parametrize("name", [_INTERP, _CONFORM])
def test_pass_fixture_is_green(name: str, tmp_path: Path) -> None:
    assert _grade(_spec(name), "pass", tmp_path) is True, f"{name} RED against its _pass fixture — matchers over-fit"


@pytest.mark.parametrize("name", [_INTERP, _CONFORM])
def test_fail_fixture_is_red(name: str, tmp_path: Path) -> None:
    assert _grade(_spec(name), "fail", tmp_path) is False, f"{name} stayed GREEN against its _fail fixture — toothless"


@pytest.mark.parametrize("name", [_INTERP, _CONFORM])
def test_noop_fixture_cannot_satisfy(name: str, tmp_path: Path) -> None:
    assert _grade(_spec(name), "noop", tmp_path) is False, f"{name} is satisfied by a no-op transcript — vacuous"


@pytest.mark.parametrize("name", [_INTERP, _CONFORM])
def test_removing_matchers_turns_fail_green(name: str, tmp_path: Path) -> None:
    toothless = dataclasses.replace(_spec(name), matchers=())
    assert _grade(toothless, "fail", tmp_path) is True, (
        f"with the matchers removed {name}'s _fail fixture must go GREEN — else it fails for a "
        "reason unrelated to the matchers and the teeth proof is moot"
    )


def test_lanes() -> None:
    assert _spec(_INTERP).lane == "clean_room"
    assert _spec(_CONFORM).lane == "under_load"


def test_interpretation_prompt_tracks_the_live_contract() -> None:
    expected = build_interpreter_contract(Directive(raw_text=PROOF_CASE_TEXT, scope_overlay=SCOPE))
    assert _spec(_INTERP).prompt == expected, (
        "the interpretation scenario's prompt drifted from build_interpreter_contract — regenerate it "
        "so a doctrine edit forces the scenario to follow"
    )


def test_interpretation_matchers_conform_to_the_exemplar_envelope() -> None:
    serialized = json.dumps(EXEMPLAR_ENVELOPE)
    finals = [m for m in _spec(_INTERP).matchers if isinstance(m, FinalStateMatcher)]
    assert len(finals) == 4, f"expected 4 final_state matchers, found {len(finals)}"
    for matcher in finals:
        assert matcher.operator == "~", "each interpretation matcher must be a regex (~)"
        assert re.search(matcher.value, serialized), (
            f"matcher {matcher.value!r} does not match the serialized EXEMPLAR_ENVELOPE — the dogfood "
            "fixture and the eval's expected answer have diverged"
        )
