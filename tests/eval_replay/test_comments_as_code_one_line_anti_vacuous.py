"""Anti-vacuity + isolated-teeth replay for the two ``comments_as_code_one_line`` scenarios.

The pair pins the comments-as-code rule from both sides: the AUTHOR must write one-line
comments only (never a banner block narrating the function), and the REVIEWER must flag
such a block on a diff while leaving the legitimate one-line *why* — and the untouched
pre-existing comment — alone.

Each scenario's discriminating tooth is its LAST matcher; the first is only a positive
anchor that both a compliant and a non-compliant transcript reach. Four deterministic
trip-wires per scenario (no live model, run every commit):

*   the ``_pass`` fixture grades GREEN (matchers are not over-fit);
*   the ``_fail`` fixture grades RED (the matchers have teeth);
*   removing ALL matchers turns ``_fail`` GREEN — so its RED is caused by the matchers
    and not by something unrelated, which would make the teeth proof moot;
*   removing ONLY the last matcher turns ``_fail`` GREEN while the anchor still passes —
    isolating the tooth. Both ``_fail`` fixtures deliberately satisfy the anchor (the
    author one still defines the function; the review one still says "comment"), so this
    is a real isolation rather than an artifact of the anchor failing too.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by the
# established eval-suite convention (README § "tests over those definitions").

import dataclasses
from pathlib import Path

import pytest

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import evaluate

_NAMES = ("comments_as_code_one_line_author", "comments_as_code_one_line_review")
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"


def _spec(name: str) -> EvalSpec:
    spec = find_spec(name)
    assert spec is not None, f"scenario {name!r} not discovered — check evals/scenarios/comments_as_code_one_line.yaml"
    return spec


def _grade(spec: EvalSpec, suffix: str, tmp_path: Path) -> bool:
    fixture = _FIXTURES / f"{spec.name}_{suffix}.stream.jsonl"
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


@pytest.mark.parametrize("name", _NAMES)
def test_pass_fixture_is_green(name: str, tmp_path: Path) -> None:
    assert _grade(_spec(name), "pass", tmp_path) is True, f"{name} RED against its _pass fixture — matchers over-fit"


@pytest.mark.parametrize("name", _NAMES)
def test_fail_fixture_is_red(name: str, tmp_path: Path) -> None:
    assert _grade(_spec(name), "fail", tmp_path) is False, f"{name} stayed GREEN against its _fail fixture — toothless"


@pytest.mark.parametrize("name", _NAMES)
def test_removing_matchers_turns_fail_green(name: str, tmp_path: Path) -> None:
    toothless = dataclasses.replace(_spec(name), matchers=())
    assert _grade(toothless, "fail", tmp_path) is True, (
        f"with the matchers removed {name}'s _fail fixture must go GREEN — else it fails for a "
        "reason unrelated to the matchers and the teeth proof is moot"
    )


@pytest.mark.parametrize("name", _NAMES)
def test_removing_only_the_discriminating_tooth_turns_fail_green(name: str, tmp_path: Path) -> None:
    spec = _spec(name)
    assert len(spec.matchers) >= 2, f"{name} must carry a positive anchor plus a discriminating tooth"
    anchor_only = dataclasses.replace(spec, matchers=spec.matchers[:-1])
    assert _grade(anchor_only, "fail", tmp_path) is True, (
        f"{name}'s _fail fixture must go GREEN once the last matcher is removed — the fixture "
        "satisfies the anchor, so its RED must come from the discriminating tooth alone"
    )


@pytest.mark.parametrize("name", _NAMES)
def test_lane_is_clean_room(name: str) -> None:
    assert _spec(name).lane == "clean_room"
