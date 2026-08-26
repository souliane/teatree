"""Anti-vacuity + isolated-tooth replay for ``fuzz_harness_owns_its_process_group``.

The rule the scenario pins: a probe that spawns shells owns its process group and kills it
on every exit path. The ``_fail`` fixture is the recorded leak exactly — it DOES lead a
group and it DOES deadline each case, and it still leaks, because it never signals the
group it created. So the fixture satisfies every matcher but the last, and its RED
isolates the discriminating tooth rather than merely restating the anchor.

Four deterministic trip-wires, no live model, run every commit.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by the
# established eval-suite convention (README § "tests over those definitions").

import dataclasses
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import evaluate

_NAME = "fuzz_harness_owns_its_process_group"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"
_MIN_MATCHERS = 2


def _spec() -> EvalSpec:
    spec = find_spec(_NAME)
    assert spec is not None, f"scenario {_NAME!r} not discovered — check evals/scenarios/rules.yaml"
    return spec


def _grade(spec: EvalSpec, suffix: str, tmp_path: Path) -> bool:
    fixture = _FIXTURES / f"{_NAME}_{suffix}.stream.jsonl"
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    return evaluate(spec, TranscriptRunner(transcript_dir=tmp_path).run(spec)).passed


def test_pass_fixture_is_green(tmp_path: Path) -> None:
    assert _grade(_spec(), "pass", tmp_path) is True, "RED against its _pass fixture — matchers over-fit"


def test_fail_fixture_is_red(tmp_path: Path) -> None:
    assert _grade(_spec(), "fail", tmp_path) is False, "stayed GREEN against its _fail fixture — toothless"


def test_removing_matchers_turns_fail_green(tmp_path: Path) -> None:
    toothless = dataclasses.replace(_spec(), matchers=())
    assert _grade(toothless, "fail", tmp_path) is True, (
        "with the matchers removed the _fail fixture must go GREEN — else it fails for a "
        "reason unrelated to the matchers and the teeth proof is moot"
    )


def test_removing_only_the_discriminating_tooth_turns_fail_green(tmp_path: Path) -> None:
    spec = _spec()
    assert len(spec.matchers) >= _MIN_MATCHERS, "must carry a positive anchor plus a discriminating tooth"
    anchor_only = dataclasses.replace(spec, matchers=spec.matchers[:-1])
    assert _grade(anchor_only, "fail", tmp_path) is True, (
        "the _fail fixture must go GREEN once the last matcher is removed — it leads a group "
        "and deadlines each case, so its RED must come from the unkilled group alone"
    )


def test_lane_is_clean_room() -> None:
    assert _spec().lane == "clean_room"
