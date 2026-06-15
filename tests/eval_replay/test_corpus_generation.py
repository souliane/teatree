"""The committed generated corpus matches its declaration.

The themed scenario YAML under ``evals/scenarios/`` and their
``stream-json`` fixtures under ``evals/fixtures/`` are emitted from the
declaration in ``scripts/eval/corpus_gen``. This test re-runs the emitter in
memory and asserts every planned file is committed with identical content, so a
catalog edit without a regenerate (or a hand-edit of a generated file) fails CI
instead of shipping drift.

It also re-checks the anti-vacuous contract directly from the declaration: each
scenario's ``_pass`` fixture grades GREEN, its ``_fail`` fixture grades RED, and
(when it has a negative matcher) its ``_noop`` fixture grades RED — the same
guarantee the on-disk anti-vacuous gate enforces, here pinned at the source.
"""

from pathlib import Path

import pytest

from scripts.eval.corpus_gen.all_scenarios import ALL_SCENARIOS
from scripts.eval.corpus_gen.model import Scenario, fixture_stream
from scripts.eval.generate_corpus import planned_files
from teatree.eval.backends import SubscriptionTranscriptRunner
from teatree.eval.loader import load_eval_yaml
from teatree.eval.report import evaluate


def _grade(scenario: Scenario, variant: str, tmp_path: Path) -> bool:
    spec_path = tmp_path / f"{scenario.name}.yaml"
    from scripts.eval.corpus_gen.model import scenario_yaml  # noqa: PLC0415

    spec_path.write_text(scenario_yaml(scenario), encoding="utf-8")
    spec = load_eval_yaml(spec_path)[0]
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture_stream(scenario, variant), encoding="utf-8")
    run = SubscriptionTranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


def test_committed_files_match_declaration() -> None:
    yaml_files, fixture_files = planned_files()
    planned = {**yaml_files, **fixture_files}
    assert planned, "the catalog declared no files"
    mismatched: list[str] = []
    for path, expected in planned.items():
        if not path.is_file():
            mismatched.append(f"missing: {path}")
        elif path.read_text(encoding="utf-8") != expected:
            mismatched.append(f"stale: {path}")
    assert not mismatched, (
        "generated corpus is out of date with scripts/eval/corpus_gen — run "
        "`uv run python scripts/eval/generate_corpus.py`:\n  " + "\n  ".join(mismatched)
    )


def test_scenario_names_are_unique() -> None:
    names = [s.name for s in ALL_SCENARIOS]
    assert len(names) == len(set(names))


class TestAgentSectionsEmission:
    @staticmethod
    def _scenario(*, agent_sections: tuple[str, ...] = ()) -> Scenario:
        from scripts.eval.corpus_gen.model import Call, match, positive  # noqa: PLC0415

        return Scenario(
            name="scoped",
            scenario="a scoped scenario",
            agent_path="skills/rules/SKILL.md",
            prompt="do the thing",
            expects=(
                positive(
                    match("Bash", "command", "x"),
                    pass_call=Call(tool="Bash", args={"command": "x here"}),
                    fail_call=Call(tool="Bash", args={"command": "nope"}),
                ),
            ),
            agent_sections=agent_sections,
        )

    def test_agent_sections_renders_a_yaml_list_the_loader_accepts(self, tmp_path: Path) -> None:
        from scripts.eval.corpus_gen.model import scenario_yaml  # noqa: PLC0415

        scenario = self._scenario(agent_sections=("Background Long Operations (Non-Negotiable)",))
        spec_path = tmp_path / "scoped.yaml"
        spec_path.write_text(scenario_yaml(scenario), encoding="utf-8")
        spec = load_eval_yaml(spec_path)[0]
        assert spec.agent_sections == ("Background Long Operations (Non-Negotiable)",)

    def test_no_agent_sections_omits_the_field(self) -> None:
        from scripts.eval.corpus_gen.model import scenario_yaml  # noqa: PLC0415

        assert "agent_sections" not in scenario_yaml(self._scenario())


@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=lambda s: s.name)
class TestDeclarationIsAntiVacuous:
    def test_pass_fixture_grades_green(self, scenario: Scenario, tmp_path: Path) -> None:
        assert scenario.has_positive, f"{scenario.name} has no positive matcher to satisfy"
        assert _grade(scenario, "pass", tmp_path) is True

    def test_fail_fixture_grades_red(self, scenario: Scenario, tmp_path: Path) -> None:
        assert _grade(scenario, "fail", tmp_path) is False

    def test_noop_fixture_grades_red_when_negative(self, scenario: Scenario, tmp_path: Path) -> None:
        if not scenario.has_negative:
            pytest.skip("no negative matcher; noop fixture not emitted")
        assert _grade(scenario, "noop", tmp_path) is False


def _grade_transcript(scenario: Scenario, transcript: str, tmp_path: Path) -> bool:
    from scripts.eval.corpus_gen.model import scenario_yaml  # noqa: PLC0415

    spec_path = tmp_path / f"{scenario.name}.yaml"
    spec_path.write_text(scenario_yaml(scenario), encoding="utf-8")
    spec = load_eval_yaml(spec_path)[0]
    (tmp_path / f"{spec.name}.jsonl").write_text(transcript, encoding="utf-8")
    run = SubscriptionTranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


def _monitor_transcript(scenario_name: str, command: str) -> str:
    from scripts.eval.corpus_gen.model import Call, _event, _init, _result, _text  # noqa: PLC0415

    call = Call(tool="Monitor", args={"command": command})
    lines = [_init(f"fixt-{scenario_name}-monitor"), _text("arming a monitor."), _event(call, 1), _result()]
    return "\n".join(lines) + "\n"


def _background_scenario_named(name: str) -> Scenario:
    return next(s for s in ALL_SCENARIOS if s.name == name)


class TestMonitorBranchRejectsNonWatch:
    """The Monitor ``any_of`` branch matches a REAL watch command, not any prose.

    A too-loose keyword (e.g. ``(?i)(ci|pipeline|gh run|glab)``) let ``echo
    pipeline`` satisfy the branch and ``ci`` match inside ``decision`` — a wrong
    response false-passing. These pin that a non-watch Monitor grades RED while a
    real ``gh run watch`` grades GREEN.
    """

    def test_echo_pipeline_monitor_does_not_satisfy_the_branch(self, tmp_path: Path) -> None:
        scenario = _background_scenario_named("never_foreground_poll_ci_pipeline")
        transcript = _monitor_transcript(scenario.name, "echo pipeline")
        assert _grade_transcript(scenario, transcript, tmp_path) is False

    def test_ci_inside_an_unrelated_word_does_not_satisfy_the_branch(self, tmp_path: Path) -> None:
        scenario = _background_scenario_named("never_foreground_poll_ci_pipeline")
        transcript = _monitor_transcript(scenario.name, "make a decision about the build")
        assert _grade_transcript(scenario, transcript, tmp_path) is False

    def test_real_gh_run_watch_monitor_satisfies_the_branch(self, tmp_path: Path) -> None:
        scenario = _background_scenario_named("never_foreground_poll_ci_pipeline")
        transcript = _monitor_transcript(scenario.name, "gh run watch")
        assert _grade_transcript(scenario, transcript, tmp_path) is True

    def test_real_until_gh_run_loop_monitor_satisfies_the_branch(self, tmp_path: Path) -> None:
        scenario = _background_scenario_named("never_foreground_poll_ci_pipeline")
        transcript = _monitor_transcript(scenario.name, "until gh run list | grep -q completed; do sleep 10; done")
        assert _grade_transcript(scenario, transcript, tmp_path) is True
