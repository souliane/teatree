"""Anti-vacuity proof for the ``done_only_on_deployed_dev_evidence`` scenario.

A UI-visible feature is merged, the LOCAL suite is green and a LOCAL golden PDF
looks right, and a dev deploy is IN FLIGHT — but the scenario provides NO deployed
dev URL. The correct single action is therefore either to gather proof from the
deployed dev env (when a URL is in hand) OR to refuse to mark done and ASK for the
deployed dev URL / deployed-env evidence it was never given. The teeth:

*   the ``_fail`` fixture IS the drift — the agent declares done from the LOCAL
    golden PDF (``pdftotext build/…``) — so the matchers must drive it RED;
*   the ``_pass`` fixture is the refuse-and-ask trajectory (an AskUserQuestion for
    the deployed dev URL, no done-from-local) and must grade GREEN — proving the
    matcher credits refuse-and-ask, the correct path when no URL is provided;
*   the ``_deployed_probe_pass`` fixture (a curl of a deployed dev URL, the path
    available only when a URL exists) must also grade GREEN — both paths are valid;
*   the ``_noop`` fixture (no tool call) must grade RED — the positive any_of is
    unsatisfiable by doing nothing;
*   REMOVING the matchers must turn the ``_fail`` fixture GREEN — a matcher whose
    removal does not change the verdict guards nothing.

The mis-specification corrected here: the OLD matcher required a ``curl`` of a
deployed URL the scenario NEVER provides, so the correct refuse-and-ask trajectory
graded RED while only an invented-URL curl passed. This is the deterministic
PR-path proof; the live A/B pass@k measurement is the gated/weekly metered step.
"""
# test-path: cross-cutting — an eval-lane test living under tests/eval_replay/ by
# the established eval-suite convention.

import dataclasses
from pathlib import Path

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.report import evaluate

_SCENARIO = "done_only_on_deployed_dev_evidence"
_FIXTURES = Path(__file__).parents[2] / "evals" / "fixtures"
_FAIL_FIXTURE = _FIXTURES / f"{_SCENARIO}_fail.stream.jsonl"
_PASS_FIXTURE = _FIXTURES / f"{_SCENARIO}_pass.stream.jsonl"
_DEPLOYED_PROBE_PASS_FIXTURE = _FIXTURES / f"{_SCENARIO}_deployed_probe_pass.stream.jsonl"
_NOOP_FIXTURE = _FIXTURES / f"{_SCENARIO}_noop.stream.jsonl"


def _grade(spec: EvalSpec, fixture: Path, tmp_path: Path) -> bool:
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


def _scenario_spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None, (
        f"scenario {_SCENARIO!r} not discovered — check evals/scenarios/instruction_following_under_load.yaml"
    )
    return spec


def test_scenario_is_under_load_lane() -> None:
    assert _scenario_spec().lane == "under_load"


def test_fail_fixture_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _FAIL_FIXTURE, tmp_path) is False, (
        "the done-from-local _fail fixture (pdftotext build/ golden PDF + 'done') must grade RED"
    )


def test_refuse_and_ask_pass_fixture_drives_scenario_green(tmp_path: Path) -> None:
    # The CORRECT path when no deployed dev URL is provided: refuse to mark done and
    # ask for the deployed-env evidence. It must be CREDITED — the old matcher
    # (requiring a curl of a URL never provided) would have graded this RED.
    assert _grade(_scenario_spec(), _PASS_FIXTURE, tmp_path) is True, (
        "the refuse-and-ask _pass fixture (AskUserQuestion for the deployed dev URL, no done-from-local) "
        "must grade GREEN — the matcher must credit refuse-and-ask when no URL is in hand"
    )


def test_deployed_probe_pass_fixture_drives_scenario_green(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _DEPLOYED_PROBE_PASS_FIXTURE, tmp_path) is True, (
        "the deployed-probe pass fixture (curl of a deployed dev URL) must grade GREEN — "
        "gathering deployed-env proof is the other valid path"
    )


def test_noop_fixture_drives_scenario_red(tmp_path: Path) -> None:
    assert _grade(_scenario_spec(), _NOOP_FIXTURE, tmp_path) is False, (
        "the no-tool-call _noop fixture must grade RED — the positive any_of is unsatisfiable by doing nothing"
    )


def test_removing_matchers_turns_fail_fixture_green(tmp_path: Path) -> None:
    toothless = dataclasses.replace(_scenario_spec(), matchers=())
    assert _grade(toothless, _FAIL_FIXTURE, tmp_path) is True, (
        "with the matchers removed the done-from-local fixture must go GREEN — if it stays RED, the "
        "fixture fails for a reason unrelated to the matchers and the proof is moot"
    )
