from pathlib import Path

import pytest

from teatree.eval.loader import EvalSpecError, load_eval_yaml
from teatree.eval.models import DEFAULT_MAX_TURNS, AnyOf, EvalRun, EvalToolCall, FinalStateMatcher
from teatree.eval.report import evaluate

_MINIMAL = (
    "- name: example\n"
    "  scenario: example scenario\n"
    "  prompt: do the thing\n"
    "  expect:\n"
    "    - tool_call: bash\n"
    '      args.command: contains "git worktree add"\n'
)


def _write(tmp_path: Path, body: str) -> Path:
    target = tmp_path / "spec.yaml"
    target.write_text(body, encoding="utf-8")
    return target


def _run(*calls: EvalToolCall) -> EvalRun:
    return EvalRun(
        spec_name="neg_contains",
        tool_calls=calls,
        text_blocks=(),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )


class TestLoadEvalYaml:
    def test_loads_one_spec_with_required_fields(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _MINIMAL)
        specs = load_eval_yaml(path)
        assert len(specs) == 1
        spec = specs[0]
        assert spec.name == "example"
        assert spec.scenario == "example scenario"
        assert spec.prompt == "do the thing"

    def test_defaults_model_tier_and_phase_to_unset(self, tmp_path: Path) -> None:
        # A scenario that declares none of model/tier/phase leaves all three unset;
        # the runner resolves it to the DEFAULT_TIER model. No concrete id default.
        spec = load_eval_yaml(_write(tmp_path, _MINIMAL))[0]
        assert spec.model == ""
        assert spec.tier == ""
        assert spec.phase == ""

    def test_parses_tier(self, tmp_path: Path) -> None:
        body = _MINIMAL.replace("  prompt: do the thing\n", "  prompt: do the thing\n  tier: frontier\n")
        spec = load_eval_yaml(_write(tmp_path, body))[0]
        assert spec.tier == "frontier"
        assert spec.model == ""

    def test_parses_phase(self, tmp_path: Path) -> None:
        body = _MINIMAL.replace("  prompt: do the thing\n", "  prompt: do the thing\n  phase: planning\n")
        spec = load_eval_yaml(_write(tmp_path, body))[0]
        assert spec.phase == "planning"

    def test_unknown_tier_fails_loud(self, tmp_path: Path) -> None:
        body = _MINIMAL.replace("  prompt: do the thing\n", "  prompt: do the thing\n  tier: gold\n")
        with pytest.raises(EvalSpecError, match="tier"):
            load_eval_yaml(_write(tmp_path, body))

    def test_blank_phase_fails_loud(self, tmp_path: Path) -> None:
        body = _MINIMAL.replace("  prompt: do the thing\n", '  prompt: do the thing\n  phase: "  "\n')
        with pytest.raises(EvalSpecError, match="phase"):
            load_eval_yaml(_write(tmp_path, body))

    def test_defaults_max_turns_to_the_generous_default(self, tmp_path: Path) -> None:
        # A scenario that declares no max_turns gets the GENEROUS lane default —
        # the old floor of 4 force-FAILed multi-step / delegating scenarios.
        spec = load_eval_yaml(_write(tmp_path, _MINIMAL))[0]
        assert spec.max_turns == DEFAULT_MAX_TURNS
        assert DEFAULT_MAX_TURNS >= 20

    def test_defaults_tools_to_bash(self, tmp_path: Path) -> None:
        spec = load_eval_yaml(_write(tmp_path, _MINIMAL))[0]
        assert spec.tools == ("Bash",)

    def test_defaults_per_scenario_caps_to_none(self, tmp_path: Path) -> None:
        # A scenario that declares no per-scenario cap defers to the run/lane default
        # (the override is None), so existing scenarios are unchanged.
        spec = load_eval_yaml(_write(tmp_path, _MINIMAL))[0]
        assert spec.max_budget_usd is None
        assert spec.watchdog_seconds is None

    def test_parses_per_scenario_budget_and_watchdog(self, tmp_path: Path) -> None:
        # The cap-relief overrides: a delegation scenario raises both to FIT a
        # legitimate sub-agent TDD cycle without widening the shared default.
        body = (
            "- name: example\n"
            "  scenario: example scenario\n"
            "  prompt: do the thing\n"
            "  max_budget_usd: 4.0\n"
            "  watchdog_seconds: 600\n"
            "  expect:\n"
            "    - tool_call: bash\n"
            '      args.command: contains "git worktree add"\n'
        )
        spec = load_eval_yaml(_write(tmp_path, body))[0]
        assert spec.max_budget_usd == pytest.approx(4.0)
        assert spec.watchdog_seconds == pytest.approx(600.0)

    def test_rejects_non_positive_budget(self, tmp_path: Path) -> None:
        # A fat-fingered 0 must be a spec error, never a silent tighten-to-nothing.
        body = (
            "- name: example\n"
            "  scenario: example scenario\n"
            "  prompt: do the thing\n"
            "  max_budget_usd: 0\n"
            "  expect:\n"
            "    - tool_call: bash\n"
            '      args.command: contains "git worktree add"\n'
        )
        with pytest.raises(EvalSpecError, match="max_budget_usd"):
            load_eval_yaml(_write(tmp_path, body))

    def test_rejects_non_numeric_watchdog(self, tmp_path: Path) -> None:
        body = (
            "- name: example\n"
            "  scenario: example scenario\n"
            "  prompt: do the thing\n"
            "  watchdog_seconds: soon\n"
            "  expect:\n"
            "    - tool_call: bash\n"
            '      args.command: contains "git worktree add"\n'
        )
        with pytest.raises(EvalSpecError, match="watchdog_seconds"):
            load_eval_yaml(_write(tmp_path, body))

    def test_overrides_model_max_turns_and_tools(self, tmp_path: Path) -> None:
        body = (
            "- name: tuned\n"
            "  scenario: tuned scenario\n"
            "  prompt: do the thing\n"
            "  model: sonnet\n"
            "  max_turns: 7\n"
            "  tools: [Bash, Read]\n"
            "  expect:\n"
            "    - tool_call: bash\n"
            '      args.command: contains "x"\n'
        )
        spec = load_eval_yaml(_write(tmp_path, body))[0]
        assert spec.model == "sonnet"
        assert spec.max_turns == 7
        assert spec.tools == ("Bash", "Read")

    def test_uses_agent_path_field(self, tmp_path: Path) -> None:
        body = (
            "- name: agent_path_test\n"
            "  scenario: agent path\n"
            "  agent_path: skills/ship/SKILL.md\n"
            "  prompt: do the thing\n"
            "  expect:\n"
            "    - tool_call: bash\n"
            '      args.command: contains "x"\n'
        )
        spec = load_eval_yaml(_write(tmp_path, body))[0]
        assert spec.agent_path == "skills/ship/SKILL.md"

    def test_defaults_agent_path_to_code_skill(self, tmp_path: Path) -> None:
        spec = load_eval_yaml(_write(tmp_path, _MINIMAL))[0]
        assert spec.agent_path == "skills/code/SKILL.md"

    def test_defaults_agent_sections_to_empty(self, tmp_path: Path) -> None:
        spec = load_eval_yaml(_write(tmp_path, _MINIMAL))[0]
        assert spec.agent_sections == ()

    def test_parses_agent_sections_list(self, tmp_path: Path) -> None:
        body = (
            "- name: scoped\n"
            "  scenario: scoped scenario\n"
            "  agent_path: skills/rules/SKILL.md\n"
            "  agent_sections:\n"
            "    - Background Long Operations\n"
            "    - Worktree-First Work\n"
            "  prompt: do the thing\n"
            "  expect:\n"
            "    - tool_call: bash\n"
            '      args.command: contains "x"\n'
        )
        spec = load_eval_yaml(_write(tmp_path, body))[0]
        assert spec.agent_sections == ("Background Long Operations", "Worktree-First Work")

    def test_rejects_empty_agent_sections(self, tmp_path: Path) -> None:
        body = (
            "- name: bad\n"
            "  scenario: bad\n"
            "  agent_sections: []\n"
            "  prompt: x\n"
            "  expect:\n"
            "    - tool_call: bash\n"
            '      args.command: contains "x"\n'
        )
        with pytest.raises(EvalSpecError, match="agent_sections"):
            load_eval_yaml(_write(tmp_path, body))

    def test_rejects_non_string_agent_sections(self, tmp_path: Path) -> None:
        body = (
            "- name: bad\n"
            "  scenario: bad\n"
            "  agent_sections: [123]\n"
            "  prompt: x\n"
            "  expect:\n"
            "    - tool_call: bash\n"
            '      args.command: contains "x"\n'
        )
        with pytest.raises(EvalSpecError, match="agent_sections"):
            load_eval_yaml(_write(tmp_path, body))

    def test_defaults_lane_to_clean_room(self, tmp_path: Path) -> None:
        spec = load_eval_yaml(_write(tmp_path, _MINIMAL))[0]
        assert spec.lane == "clean_room"

    def test_defaults_context_preamble_to_empty(self, tmp_path: Path) -> None:
        spec = load_eval_yaml(_write(tmp_path, _MINIMAL))[0]
        assert spec.context_preamble == ""

    def test_parses_under_load_lane_and_context_preamble(self, tmp_path: Path) -> None:
        body = (
            "- name: drift\n"
            "  scenario: drift scenario\n"
            "  lane: under_load\n"
            "  context_preamble: a wall of polluted context\n"
            "  prompt: do the thing\n"
            "  expect:\n"
            "    - tool_call: bash\n"
            '      args.command: contains "x"\n'
        )
        spec = load_eval_yaml(_write(tmp_path, body))[0]
        assert spec.lane == "under_load"
        assert spec.context_preamble == "a wall of polluted context"

    def test_rejects_unknown_lane(self, tmp_path: Path) -> None:
        body = (
            "- name: bad\n"
            "  scenario: bad\n"
            "  lane: heavy_load\n"
            "  prompt: x\n"
            "  expect:\n"
            "    - tool_call: bash\n"
            '      args.command: contains "x"\n'
        )
        with pytest.raises(EvalSpecError, match="lane"):
            load_eval_yaml(_write(tmp_path, body))

    def test_default_agent_path_overrides_global_default_when_omitted(self, tmp_path: Path) -> None:
        spec = load_eval_yaml(_write(tmp_path, _MINIMAL), default_agent_path="skills/ship/SKILL.md")[0]
        assert spec.agent_path == "skills/ship/SKILL.md"

    def test_explicit_agent_path_wins_over_default_agent_path(self, tmp_path: Path) -> None:
        body = (
            "- name: explicit\n"
            "  scenario: explicit agent\n"
            "  agent_path: skills/review/SKILL.md\n"
            "  prompt: do the thing\n"
            "  expect:\n"
            "    - tool_call: bash\n"
            '      args.command: contains "x"\n'
        )
        spec = load_eval_yaml(_write(tmp_path, body), default_agent_path="skills/ship/SKILL.md")[0]
        assert spec.agent_path == "skills/review/SKILL.md"

    def test_parses_positive_matcher(self, tmp_path: Path) -> None:
        spec = load_eval_yaml(_write(tmp_path, _MINIMAL))[0]
        matcher = spec.matchers[0]
        assert matcher.kind == "positive"
        assert matcher.tool == "bash"
        assert matcher.arg_path == "command"
        assert matcher.operator == "contains"
        assert matcher.value == "git worktree add"

    def test_parses_negative_matcher(self, tmp_path: Path) -> None:
        body = (
            "- name: neg\n"
            "  scenario: negative\n"
            "  prompt: do the thing\n"
            "  expect:\n"
            "    - no_tool_call_matching:\n"
            '        bash.command: ~ "rm -rf"\n'
        )
        spec = load_eval_yaml(_write(tmp_path, body))[0]
        matcher = spec.matchers[0]
        assert matcher.kind == "negative"
        assert matcher.tool == "bash"
        assert matcher.arg_path == "command"
        assert matcher.operator == "~"
        assert matcher.value == "rm -rf"

    def test_negative_contains_yaml_round_trips_through_grader(self, tmp_path: Path) -> None:
        # Regression seam (loader -> dispatch): the loader accepts `contains` for a
        # `no_tool_call_matching` line, producing Matcher(kind="negative",
        # operator="contains"); the grader's _dispatch had no branch for that combo
        # and fell through to NotImplementedError, crashing the dream `--full` eval
        # derivation. The matcher-level and dispatch-level halves are covered
        # separately; this exercises a LOADER-produced matcher (not a hand-built
        # one) through report.evaluate in a single load -> grade round-trip.
        body = (
            "- name: neg_contains\n"
            "  scenario: forbid a drift substring\n"
            "  prompt: do the thing\n"
            "  expect:\n"
            "    - no_tool_call_matching:\n"
            '        bash.command: contains "--no-verify"\n'
        )
        spec = load_eval_yaml(_write(tmp_path, body))[0]
        matcher = spec.matchers[0]
        assert matcher.kind == "negative"
        assert matcher.operator == "contains"
        assert matcher.tool == "bash"
        assert matcher.arg_path == "command"
        assert matcher.value == "--no-verify"

        # FAIL when a matching tool call CONTAINS the forbidden substring...
        present = _run(EvalToolCall(name="Bash", input={"command": "git commit --no-verify -m x"}, turn=1))
        assert evaluate(spec, present).passed is False

        # ...PASS when it is absent. The present/absent pair proves teeth.
        absent = _run(EvalToolCall(name="Bash", input={"command": "git commit -m x"}, turn=1))
        assert evaluate(spec, absent).passed is True

    def test_rejects_empty_expect(self, tmp_path: Path) -> None:
        body = "- name: bad\n  scenario: bad\n  prompt: do the thing\n  expect: []\n"
        with pytest.raises(EvalSpecError):
            load_eval_yaml(_write(tmp_path, body))

    def test_rejects_missing_required_field(self, tmp_path: Path) -> None:
        body = (
            "- scenario: no name here\n"
            "  prompt: do\n"
            "  expect:\n"
            "    - tool_call: bash\n"
            '      args.command: contains "x"\n'
        )
        with pytest.raises(EvalSpecError):
            load_eval_yaml(_write(tmp_path, body))

    def test_rejects_non_positive_max_turns(self, tmp_path: Path) -> None:
        body = (
            "- name: bad_turns\n"
            "  scenario: bad turns\n"
            "  prompt: do\n"
            "  max_turns: 0\n"
            "  expect:\n"
            "    - tool_call: bash\n"
            '      args.command: contains "x"\n'
        )
        with pytest.raises(EvalSpecError):
            load_eval_yaml(_write(tmp_path, body))

    def test_rejects_empty_tools_list(self, tmp_path: Path) -> None:
        body = (
            "- name: bad_tools\n"
            "  scenario: bad\n"
            "  prompt: do\n"
            "  tools: []\n"
            "  expect:\n"
            "    - tool_call: bash\n"
            '      args.command: contains "x"\n'
        )
        with pytest.raises(EvalSpecError):
            load_eval_yaml(_write(tmp_path, body))

    def test_rejects_yaml_with_parse_error(self, tmp_path: Path) -> None:
        # Tabs inside a flow-style block trigger a YAML scanner error and the
        # loader must surface it as EvalSpecError with a file location.
        body = "- name: bad\n\tindent_error_here: 1\n"
        with pytest.raises(EvalSpecError):
            load_eval_yaml(_write(tmp_path, body))

    def test_rejects_top_level_non_list(self, tmp_path: Path) -> None:
        body = "name: example\nscenario: not in a list\n"
        with pytest.raises(EvalSpecError) as exc:
            load_eval_yaml(_write(tmp_path, body))
        assert "expected a top-level YAML list" in str(exc.value)

    def test_rejects_non_mapping_entry(self, tmp_path: Path) -> None:
        body = "- just a string\n"
        with pytest.raises(EvalSpecError) as exc:
            load_eval_yaml(_write(tmp_path, body))
        assert "each spec must be a mapping" in str(exc.value)

    def test_rejects_expect_entry_without_known_key(self, tmp_path: Path) -> None:
        body = "- name: bad\n  scenario: bad\n  prompt: do\n  expect:\n    - something_else: yes\n"
        with pytest.raises(EvalSpecError) as exc:
            load_eval_yaml(_write(tmp_path, body))
        assert "tool_call" in str(exc.value)

    def test_rejects_negative_without_dot_key(self, tmp_path: Path) -> None:
        body = (
            "- name: bad\n"
            "  scenario: bad\n"
            "  prompt: do\n"
            "  expect:\n"
            "    - no_tool_call_matching:\n"
            '        nodot: ~ "x"\n'
        )
        with pytest.raises(EvalSpecError) as exc:
            load_eval_yaml(_write(tmp_path, body))
        assert "<tool>.<arg>" in str(exc.value)

    def test_rejects_negative_with_multiple_inner_keys(self, tmp_path: Path) -> None:
        body = (
            "- name: bad\n"
            "  scenario: bad\n"
            "  prompt: do\n"
            "  expect:\n"
            "    - no_tool_call_matching:\n"
            '        bash.command: ~ "x"\n'
            '        bash.description: ~ "y"\n'
        )
        with pytest.raises(EvalSpecError):
            load_eval_yaml(_write(tmp_path, body))

    def test_rejects_positive_without_args_entry(self, tmp_path: Path) -> None:
        body = "- name: bad\n  scenario: bad\n  prompt: do\n  expect:\n    - tool_call: bash\n"
        with pytest.raises(EvalSpecError) as exc:
            load_eval_yaml(_write(tmp_path, body))
        assert "args." in str(exc.value)

    def test_rejects_unknown_operator(self, tmp_path: Path) -> None:
        body = (
            "- name: bad\n"
            "  scenario: bad\n"
            "  prompt: do\n"
            "  expect:\n"
            "    - tool_call: bash\n"
            '      args.command: startswith "x"\n'
        )
        with pytest.raises(EvalSpecError) as exc:
            load_eval_yaml(_write(tmp_path, body))
        assert "contains" in str(exc.value)

    def test_rejects_non_mapping_expect_entry(self, tmp_path: Path) -> None:
        body = "- name: bad\n  scenario: bad\n  prompt: do\n  expect:\n    - just a string entry\n"
        with pytest.raises(EvalSpecError) as exc:
            load_eval_yaml(_write(tmp_path, body))
        assert "expect" in str(exc.value) or "mapping" in str(exc.value)


class TestJudgeBlock:
    def test_parses_judge_with_rubric_and_defaults(self, tmp_path: Path) -> None:
        body = (
            "- name: judged\n"
            "  scenario: needs a judge\n"
            "  prompt: do\n"
            "  judge:\n"
            "    rubric: The explanation is faithful to the diff.\n"
        )
        spec = load_eval_yaml(_write(tmp_path, body))[0]
        assert spec.judge is not None
        assert spec.judge.rubric == "The explanation is faithful to the diff."
        assert spec.judge.model == "claude-sonnet-4-6"
        assert spec.judge.max_output_tokens == 512
        assert spec.matchers == ()

    def test_judge_overrides_model_and_tokens(self, tmp_path: Path) -> None:
        body = (
            "- name: judged\n"
            "  scenario: needs a judge\n"
            "  prompt: do\n"
            "  judge:\n"
            "    rubric: r\n"
            "    model: sonnet\n"
            "    max_output_tokens: 128\n"
        )
        spec = load_eval_yaml(_write(tmp_path, body))[0]
        assert spec.judge.model == "sonnet"
        assert spec.judge.max_output_tokens == 128

    def test_judge_and_matchers_coexist(self, tmp_path: Path) -> None:
        body = (
            "- name: both\n"
            "  scenario: matcher plus judge\n"
            "  prompt: do\n"
            "  expect:\n"
            "    - tool_call: bash\n"
            '      args.command: contains "x"\n'
            "  judge:\n"
            "    rubric: r\n"
        )
        spec = load_eval_yaml(_write(tmp_path, body))[0]
        assert spec.judge is not None
        assert len(spec.matchers) == 1

    def test_rejects_empty_rubric(self, tmp_path: Path) -> None:
        body = "- name: bad\n  scenario: bad\n  prompt: do\n  judge:\n    rubric: '   '\n"
        with pytest.raises(EvalSpecError) as exc:
            load_eval_yaml(_write(tmp_path, body))
        assert "rubric" in str(exc.value)

    def test_rejects_bad_max_output_tokens(self, tmp_path: Path) -> None:
        body = "- name: bad\n  scenario: bad\n  prompt: do\n  judge:\n    rubric: r\n    max_output_tokens: 0\n"
        with pytest.raises(EvalSpecError) as exc:
            load_eval_yaml(_write(tmp_path, body))
        assert "max_output_tokens" in str(exc.value)

    def test_missing_both_expect_and_judge_rejected(self, tmp_path: Path) -> None:
        body = "- name: bad\n  scenario: bad\n  prompt: do\n"
        with pytest.raises(EvalSpecError) as exc:
            load_eval_yaml(_write(tmp_path, body))
        assert "expect" in str(exc.value)


class TestAnyOfMatcher:
    def test_parses_any_of_disjunction_of_positive_branches(self, tmp_path: Path) -> None:
        body = (
            "- name: anyof\n"
            "  scenario: background the long op either way\n"
            "  prompt: do\n"
            "  expect:\n"
            "    - any_of:\n"
            "        - tool_call: Task\n"
            '          args.prompt: ~ "pytest"\n'
            "        - tool_call: Bash\n"
            '          args.run_in_background: ~ "(?i)true"\n'
        )
        spec = load_eval_yaml(_write(tmp_path, body))[0]
        item = spec.matchers[0]
        assert isinstance(item, AnyOf)
        assert len(item.alternatives) == 2
        assert item.alternatives[0].tool == "Task"
        assert item.alternatives[1].arg_path == "run_in_background"
        assert all(alt.kind == "positive" for alt in item.alternatives)

    def test_rejects_empty_any_of(self, tmp_path: Path) -> None:
        body = "- name: bad\n  scenario: bad\n  prompt: do\n  expect:\n    - any_of: []\n"
        with pytest.raises(EvalSpecError) as exc:
            load_eval_yaml(_write(tmp_path, body))
        assert "any_of" in str(exc.value)

    def test_rejects_negative_branch_in_any_of(self, tmp_path: Path) -> None:
        body = (
            "- name: bad\n"
            "  scenario: bad\n"
            "  prompt: do\n"
            "  expect:\n"
            "    - any_of:\n"
            "        - no_tool_call_matching:\n"
            '            bash.command: ~ "x"\n'
        )
        with pytest.raises(EvalSpecError) as exc:
            load_eval_yaml(_write(tmp_path, body))
        assert "tool_call" in str(exc.value)


class TestFinalStateMatcher:
    def test_parses_final_state_regex_matcher(self, tmp_path: Path) -> None:
        # A `#` in the regex needs the whole value YAML-quoted (same constraint
        # the tool-call matchers face) so YAML does not treat it as a comment.
        body = (
            "- name: ends_with_pr\n"
            "  scenario: the agent ends by reporting the opened PR\n"
            "  prompt: do\n"
            "  expect:\n"
            "    - final_state: '~ \"opened PR #\\d+\"'\n"
        )
        spec = load_eval_yaml(_write(tmp_path, body))[0]
        item = spec.matchers[0]
        assert isinstance(item, FinalStateMatcher)
        assert item.operator == "~"
        assert item.value == "opened PR #\\d+"

    def test_parses_final_state_contains_matcher(self, tmp_path: Path) -> None:
        body = (
            "- name: ends_clean\n"
            "  scenario: the agent ends with a clean summary\n"
            "  prompt: do\n"
            "  expect:\n"
            '    - final_state: contains "branch is pushed"\n'
        )
        item = load_eval_yaml(_write(tmp_path, body))[0].matchers[0]
        assert isinstance(item, FinalStateMatcher)
        assert item.operator == "contains"
        assert item.value == "branch is pushed"

    def test_rejects_final_state_with_bad_operator(self, tmp_path: Path) -> None:
        body = "- name: bad\n  scenario: bad\n  prompt: do\n  expect:\n    - final_state: equals foo\n"
        with pytest.raises(EvalSpecError) as exc:
            load_eval_yaml(_write(tmp_path, body))
        assert "final_state" in str(exc.value) or "operator" in str(exc.value)

    def test_final_state_coexists_with_tool_call(self, tmp_path: Path) -> None:
        body = (
            "- name: both\n"
            "  scenario: pushes AND reports it\n"
            "  prompt: do\n"
            "  expect:\n"
            "    - tool_call: Bash\n"
            '      args.command: ~ "git push"\n'
            '    - final_state: contains "pushed"\n'
        )
        matchers = load_eval_yaml(_write(tmp_path, body))[0].matchers
        assert len(matchers) == 2
        assert isinstance(matchers[1], FinalStateMatcher)
