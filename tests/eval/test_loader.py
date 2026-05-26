from pathlib import Path

import pytest

from teatree.eval.loader import EvalSpecError, load_eval_yaml

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


class TestLoadEvalYaml:
    def test_loads_one_spec_with_required_fields(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _MINIMAL)
        specs = load_eval_yaml(path)
        assert len(specs) == 1
        spec = specs[0]
        assert spec.name == "example"
        assert spec.scenario == "example scenario"
        assert spec.prompt == "do the thing"

    def test_defaults_model_to_haiku(self, tmp_path: Path) -> None:
        spec = load_eval_yaml(_write(tmp_path, _MINIMAL))[0]
        assert spec.model == "haiku"

    def test_defaults_max_turns_to_four(self, tmp_path: Path) -> None:
        spec = load_eval_yaml(_write(tmp_path, _MINIMAL))[0]
        assert spec.max_turns == 4

    def test_defaults_tools_to_bash(self, tmp_path: Path) -> None:
        spec = load_eval_yaml(_write(tmp_path, _MINIMAL))[0]
        assert spec.tools == ("Bash",)

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
