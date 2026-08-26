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
from scripts.eval.corpus_gen.emit import orphaned_generated_files, write_catalog
from scripts.eval.corpus_gen.model import Scenario, fixture_stream, scenario_yaml
from scripts.eval.generate_corpus import planned_files
from teatree.eval.backends import TranscriptRunner
from teatree.eval.loader import load_eval_yaml
from teatree.eval.report import evaluate


def _grade(scenario: Scenario, variant: str, tmp_path: Path) -> bool:
    spec_path = tmp_path / f"{scenario.name}.yaml"
    spec_path.write_text(scenario_yaml(scenario), encoding="utf-8")
    spec = load_eval_yaml(spec_path)[0]
    (tmp_path / f"{spec.name}.jsonl").write_text(fixture_stream(scenario, variant), encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
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


def test_committed_tree_carries_no_orphaned_generated_files() -> None:
    from scripts.eval.generate_corpus import (  # noqa: PLC0415 — deferred: import is only needed on this code path
        FIXTURES_DIR,
        SCENARIOS_DIR,
    )

    yaml_files, fixture_files = planned_files()
    orphans = orphaned_generated_files(
        set(yaml_files),
        set(fixture_files),
        scenarios_dir=SCENARIOS_DIR,
        fixtures_dir=FIXTURES_DIR,
    )
    assert orphans == [], (
        "generated files the catalog no longer declares are still committed — run "
        "`uv run python scripts/eval/generate_corpus.py`:\n  " + "\n  ".join(str(p) for p in orphans)
    )


def _demo_scenario(name: str, yaml_file: str = "demo.yaml") -> Scenario:
    from scripts.eval.corpus_gen.model import (  # noqa: PLC0415 — deferred: import is only needed on this code path
        Call,
        match,
        positive,
    )

    return Scenario(
        name=name,
        scenario=f"a {name} scenario",
        agent_path="skills/rules/SKILL.md",
        prompt="do the thing",
        expects=(
            positive(
                match("Bash", "command", "x"),
                pass_call=Call(tool="Bash", args={"command": "x here"}),
                fail_call=Call(tool="Bash", args={"command": "nope"}),
            ),
        ),
        yaml_file=yaml_file,
    )


class TestWriteCatalogPrunesWhatItNoLongerDeclares:
    """A renamed or deleted scenario must not survive on disk.

    The loader reads whatever YAML sits in ``evals/scenarios/``, so an orphan
    keeps grading a scenario the declaration dropped — and it stays invisible to
    a check that only asserts every PLANNED file is present.
    """

    def _dirs(self, tmp_path: Path) -> tuple[Path, Path]:
        scenarios_dir = tmp_path / "scenarios"
        fixtures_dir = tmp_path / "fixtures"
        scenarios_dir.mkdir()
        fixtures_dir.mkdir()
        return scenarios_dir, fixtures_dir

    def test_renaming_a_scenario_removes_the_old_yaml_and_fixtures(self, tmp_path: Path) -> None:
        scenarios_dir, fixtures_dir = self._dirs(tmp_path)
        old = _demo_scenario("old_name", yaml_file="old.yaml")
        write_catalog([old], scenarios_dir=scenarios_dir, fixtures_dir=fixtures_dir)
        assert (scenarios_dir / "old.yaml").is_file()
        assert (fixtures_dir / "old_name_pass.stream.jsonl").is_file()

        write_catalog([_demo_scenario("new_name")], scenarios_dir=scenarios_dir, fixtures_dir=fixtures_dir)

        assert not (scenarios_dir / "old.yaml").exists()
        assert not (fixtures_dir / "old_name_pass.stream.jsonl").exists()
        assert (scenarios_dir / "demo.yaml").is_file()
        assert (fixtures_dir / "new_name_pass.stream.jsonl").is_file()

    def test_handwritten_scenarios_and_fixtures_are_untouched(self, tmp_path: Path) -> None:
        scenarios_dir, fixtures_dir = self._dirs(tmp_path)
        handwritten = scenarios_dir / "handwritten.yaml"
        handwritten.write_text("- name: handwritten_scenario\n", encoding="utf-8")
        handwritten_fixture = fixtures_dir / "handwritten_scenario_pass.stream.jsonl"
        handwritten_fixture.write_text("{}\n", encoding="utf-8")

        write_catalog([_demo_scenario("new_name")], scenarios_dir=scenarios_dir, fixtures_dir=fixtures_dir)

        assert handwritten.read_text(encoding="utf-8") == "- name: handwritten_scenario\n"
        assert handwritten_fixture.is_file()


class TestAgentSectionsEmission:
    @staticmethod
    def _scenario(*, agent_sections: tuple[str, ...] = ()) -> Scenario:
        from scripts.eval.corpus_gen.model import (  # noqa: PLC0415 — deferred: import is only needed on this code path
            Call,
            match,
            positive,
        )

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
        scenario = self._scenario(agent_sections=("Background Long Operations (Non-Negotiable)",))
        spec_path = tmp_path / "scoped.yaml"
        spec_path.write_text(scenario_yaml(scenario), encoding="utf-8")
        spec = load_eval_yaml(spec_path)[0]
        assert spec.agent_sections == ("Background Long Operations (Non-Negotiable)",)

    def test_no_agent_sections_omits_the_field(self) -> None:
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
    spec_path = tmp_path / f"{scenario.name}.yaml"
    spec_path.write_text(scenario_yaml(scenario), encoding="utf-8")
    spec = load_eval_yaml(spec_path)[0]
    (tmp_path / f"{spec.name}.jsonl").write_text(transcript, encoding="utf-8")
    run = TranscriptRunner(transcript_dir=tmp_path).run(spec)
    return evaluate(spec, run).passed


def _monitor_transcript(scenario_name: str, command: str) -> str:
    from scripts.eval.corpus_gen.model import (  # noqa: PLC0415 — deferred: import is only needed on this code path
        Call,
        _event,
        _init,
        _result,
        _text,
    )

    call = Call(tool="Monitor", args={"command": command})
    lines = [_init(f"fixt-{scenario_name}-monitor"), _text("arming a monitor."), _event(call, 1), _result()]
    return "\n".join(lines) + "\n"


def _background_scenario_named(name: str) -> Scenario:
    return next(s for s in ALL_SCENARIOS if s.name == name)


def _bash_transcript(scenario_name: str, command: str) -> str:
    from scripts.eval.corpus_gen.model import (  # noqa: PLC0415 — deferred: import is only needed on this code path
        Call,
        _event,
        _init,
        _result,
        _text,
    )

    call = Call(tool="Bash", args={"command": command})
    lines = [_init(f"fixt-{scenario_name}-probe"), _text("retrieving."), _event(call, 1), _result()]
    return "\n".join(lines) + "\n"


class TestTicketRetrievalRequiresTheLinkedIssue:
    """Retrieving *an* issue is not retrieving *the* issue the prompt names.

    The matcher accepted any ``glab issue view``, so an agent that fetched an
    unrelated issue from an unrelated repository scored the same as one that read
    the linked spec — the scenario graded the tool, not the behaviour.
    """

    _NAME = "review_retrieves_ticket_before_verdict"

    def test_an_unrelated_issue_from_another_repo_grades_red(self, tmp_path: Path) -> None:
        scenario = _background_scenario_named(self._NAME)
        transcript = _bash_transcript(self._NAME, "glab issue view 51 --repo widget/widget-overlay")
        assert _grade_transcript(scenario, transcript, tmp_path) is False

    def test_the_linked_issue_grades_green(self, tmp_path: Path) -> None:
        scenario = _background_scenario_named(self._NAME)
        transcript = _bash_transcript(self._NAME, "glab issue view 142 --repo mygroup/myrepo")
        assert _grade_transcript(scenario, transcript, tmp_path) is True

    def test_the_prompt_names_the_issue_the_matcher_requires(self) -> None:
        scenario = _background_scenario_named(self._NAME)
        assert "142" in scenario.prompt


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
