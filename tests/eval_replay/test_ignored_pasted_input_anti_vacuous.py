"""Anti-vacuity + isolated-teeth replay for the ignored-pasted-input eval.

The scenario ``uses_pasted_content_never_redundant_refetch`` pins the drift the
critic's ``ignored_input`` tooth guards at runtime: given a full article pasted
into the prompt, the agent must work FROM the pasted text, never re-fetch or
re-search for content it already has.

Four deterministic trip-wires (no live model, run every commit):

*   the ``_pass`` fixture grades GREEN (a compliant agent stays green);
*   the ``_fail`` fixture grades RED (a re-fetching agent surfaces red);
*   the ``_noop`` fixture (no tool calls) cannot satisfy it (positive anchor);
*   removing the matchers turns ``_fail`` GREEN — the mandatory teeth check
    (a matcher whose removal leaves the verdict unchanged guards nothing);
*   removing ONLY the re-fetch negatives turns ``_fail`` GREEN while the positive
    anchor still passes — the ``_fail`` fixture both re-fetches AND writes the
    summary, so its RED is caused by the re-fetch negatives specifically, not by
    the absent-Write anchor. That isolates the drift-catching teeth.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention (README § "tests over those definitions").

import dataclasses
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec, Matcher
from teatree.eval.report import evaluate

_NAME = "uses_pasted_content_never_redundant_refetch"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"


def _spec() -> EvalSpec:
    spec = find_spec(_NAME)
    assert spec is not None, (
        f"scenario {_NAME!r} not discovered — check evals/scenarios/instruction_following_under_load.yaml"
    )
    return spec


def _grade(spec: EvalSpec, suffix: str, tmp_path: Path) -> bool:
    fixture = _FIXTURES / f"{spec.name}_{suffix}.stream.jsonl"
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


def test_pass_fixture_is_green(tmp_path: Path) -> None:
    assert _grade(_spec(), "pass", tmp_path) is True, f"{_NAME} RED against its _pass fixture — matchers over-fit"


def test_fail_fixture_is_red(tmp_path: Path) -> None:
    assert _grade(_spec(), "fail", tmp_path) is False, f"{_NAME} stayed GREEN against its _fail fixture — toothless"


def test_noop_fixture_cannot_satisfy(tmp_path: Path) -> None:
    assert _grade(_spec(), "noop", tmp_path) is False, f"{_NAME} is satisfied by a no-op transcript — vacuous"


def test_removing_matchers_turns_fail_green(tmp_path: Path) -> None:
    toothless = dataclasses.replace(_spec(), matchers=())
    assert _grade(toothless, "fail", tmp_path) is True, (
        f"with the matchers removed {_NAME}'s _fail fixture must go GREEN — else it fails for a "
        "reason unrelated to the matchers and the teeth proof is moot"
    )


def test_removing_only_refetch_negatives_turns_fail_green(tmp_path: Path) -> None:
    """Isolate the drift-catching teeth: the negatives alone turn ``_fail`` red.

    The ``_fail`` fixture BOTH re-fetches the source AND writes the summary, so
    the positive anchor (the Write matcher) passes against it. Dropping only the
    negative re-fetch matchers must therefore turn it GREEN — proving those
    negatives, not the anchor, are what catch the ignored-pasted-input drift.
    """
    spec = _spec()
    anchor_only = tuple(m for m in spec.matchers if not (isinstance(m, Matcher) and m.kind == "negative"))
    assert anchor_only, "expected at least the positive any_of anchor to survive filtering the negatives"
    assert anchor_only != spec.matchers, "expected the scenario to carry negative re-fetch matchers to strip"
    without_negatives = dataclasses.replace(spec, matchers=anchor_only)
    assert _grade(without_negatives, "fail", tmp_path) is True, (
        f"{_NAME}'s _fail fixture must go GREEN once the re-fetch negatives are removed — the _fail "
        "fixture writes the summary, so its RED must come from the re-fetch negatives, not the anchor"
    )


def test_lane_is_under_load() -> None:
    assert _spec().lane == "under_load"
