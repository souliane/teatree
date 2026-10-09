"""Anti-vacuity + isolated-teeth replay for ``retro_finding_not_a_memory``.

The scenario grades one rule: a retro finding whose skill is read-only is emitted
with ``t3 <overlay> retro finding``, never appended to a memory file. Its
discriminating tooth is the LAST matcher — the negative that forbids the
write-a-memory remediation; the first is a positive anchor that a compliant AND a
non-compliant transcript both reach, so the ``_fail`` fixture deliberately runs a
``retro finding --dry-run`` before reaching for the memory file.

Five deterministic trip-wires, no live model, run every commit:

*   the ``_pass`` fixture grades GREEN (the matchers are not over-fit);
*   the ``_fail`` fixture grades RED (they have teeth);
*   the ``_noop`` fixture grades RED (narrating a decision is not persisting one);
*   removing ALL matchers turns ``_fail`` GREEN — so its RED comes from the
    matchers rather than something unrelated, which would make the proof moot;
*   removing ONLY the last matcher turns ``_fail`` GREEN while the anchor still
    passes — isolating the tooth.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by the
# established eval-suite convention (README § "tests over those definitions").

import dataclasses
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import evaluate

_NAME = "retro_finding_not_a_memory"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"


def _spec() -> EvalSpec:
    spec = find_spec(_NAME)
    assert spec is not None, f"scenario {_NAME!r} not discovered — check evals/scenarios/retro.yaml"
    return spec


def _grade(spec: EvalSpec, suffix: str, tmp_path: Path) -> bool:
    fixture = _FIXTURES / f"{_NAME}_{suffix}.stream.jsonl"
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    return evaluate(spec, TranscriptRunner(transcript_dir=tmp_path).run(spec)).passed


def test_pass_fixture_is_green(tmp_path: Path) -> None:
    assert _grade(_spec(), "pass", tmp_path) is True, f"{_NAME} RED against its _pass fixture — matchers over-fit"


def test_fail_fixture_is_red(tmp_path: Path) -> None:
    assert _grade(_spec(), "fail", tmp_path) is False, f"{_NAME} stayed GREEN against its _fail fixture — toothless"


def test_noop_fixture_is_red(tmp_path: Path) -> None:
    assert _grade(_spec(), "noop", tmp_path) is False, (
        f"{_NAME} stayed GREEN against a transcript that only narrates — persisting a finding is an action"
    )


def test_removing_matchers_turns_fail_green(tmp_path: Path) -> None:
    toothless = dataclasses.replace(_spec(), matchers=())
    assert _grade(toothless, "fail", tmp_path) is True, (
        f"with the matchers removed {_NAME}'s _fail fixture must go GREEN — else it fails for a "
        "reason unrelated to the matchers and the teeth proof is moot"
    )


def test_removing_only_the_discriminating_tooth_turns_fail_green(tmp_path: Path) -> None:
    spec = _spec()
    assert len(spec.matchers) >= 2, f"{_NAME} must carry a positive anchor plus a discriminating tooth"
    anchor_only = dataclasses.replace(spec, matchers=spec.matchers[:-1])
    assert _grade(anchor_only, "fail", tmp_path) is True, (
        f"{_NAME}'s _fail fixture must go GREEN once the last matcher is removed — the fixture "
        "satisfies the anchor, so its RED must come from the discriminating tooth alone"
    )
