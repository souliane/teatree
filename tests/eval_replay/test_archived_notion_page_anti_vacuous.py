"""Anti-vacuity proof for the archived-Notion-page scenario.

``archived_notion_page_is_not_a_source`` pins the moment that cost a full day
across several agents: ``t3 notion fetch`` refused a page as archived, and the
agent already HELD that page's content from an earlier connector read. The rule
is that the content it holds is not a source at all — go find the more recent
version. The teeth checks:

*   the ``_fail`` fixture IS the observed drift (the agent authors the E2E spec
    that only the dead page's ``AC-8`` demands — wrong work, not merely wasted
    work), so the matchers must drive it RED;
*   REMOVING the matchers must turn that same fixture GREEN — a matcher whose
    removal does not change the verdict guards nothing;
*   a DISCRIMINATING re-read fixture (reaching back to the dead page through the
    ``--archived-audit`` escape to "just check the AC") must ALSO grade RED, so
    the scenario rejects recovering the requirements through the audit door and
    not only the write-it-anyway case; and
*   the ``_pass`` fixture (reading the current version the refusal named) grades
    GREEN.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import evaluate

_SCENARIO = "archived_notion_page_is_not_a_source"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"
_FAIL_FIXTURE = _FIXTURES / f"{_SCENARIO}_fail.stream.jsonl"
_PASS_FIXTURE = _FIXTURES / f"{_SCENARIO}_pass.stream.jsonl"
#: The discriminator: the agent makes no edit at all, but reaches back to the dead
#: page with the audit escape to recover its acceptance criteria. The audit door is
#: for a postmortem, never for getting the requirements back, so this must grade RED
#: even though it authored nothing.
_REREAD_FAIL_FIXTURE = _FIXTURES / f"{_SCENARIO}_reread_fail.stream.jsonl"


def _grade(spec: EvalSpec, fixture: Path, tmp_path: Path) -> bool:
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


def _scenario_spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, f"scenario {_SCENARIO!r} not discovered"
    return spec


def test_building_from_the_dead_pages_criteria_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _FAIL_FIXTURE, tmp_path) is False, (
        "the _fail fixture authors the spec that only the archived page's AC-8 demands — it must grade RED"
    )


def test_reading_the_current_version_drives_scenario_green(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _PASS_FIXTURE, tmp_path) is True, (
        "reading the version the refusal named is the compliant action and must grade GREEN"
    )


def test_reaching_back_to_the_dead_page_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _REREAD_FAIL_FIXTURE, tmp_path) is False, (
        "re-reading the archived page through `--archived-audit` to recover its acceptance criteria must "
        "grade RED — the audit escape is for a postmortem, not for getting the requirements back"
    )


def test_removing_the_matchers_turns_the_fail_fixture_green(tmp_path: Path) -> None:
    toothless = dataclasses.replace(_scenario_spec(), matchers=())
    assert _grade(toothless, _FAIL_FIXTURE, tmp_path) is True, (
        "with the matchers removed the drift fixture must go GREEN — if it stays RED, the fixture fails "
        "for a reason unrelated to the matchers and the proof is moot"
    )
