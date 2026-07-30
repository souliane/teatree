"""Loader validation for `production_hooks`, `fixture`, and the on-behalf stub."""

from pathlib import Path

import pytest

from teatree.eval.loader import EvalSpecError, load_eval_yaml

_BASE = """\
- name: sample
  scenario: a scenario
  agent_path: skills/rules/SKILL.md
  tier: balanced
  tools: [Bash]
  prompt: do the thing
{extra}  expect:
    - tool_call: Bash
      args.command: '~ "."'
"""


def _write(tmp_path: Path, extra: str) -> Path:
    path = tmp_path / "spec.yaml"
    path.write_text(_BASE.format(extra=extra), encoding="utf-8")
    return path


def test_production_hooks_true_is_parsed(tmp_path: Path) -> None:
    spec = load_eval_yaml(_write(tmp_path, "  production_hooks: true\n"))[0]
    assert spec.production_hooks is True


def test_production_hooks_absent_defaults_false(tmp_path: Path) -> None:
    spec = load_eval_yaml(_write(tmp_path, ""))[0]
    assert spec.production_hooks is False


def test_production_hooks_non_bool_is_a_loud_spec_error(tmp_path: Path) -> None:
    with pytest.raises(EvalSpecError, match=r"production_hooks.*boolean"):
        load_eval_yaml(_write(tmp_path, '  production_hooks: "yes"\n'))


def test_e2e_sibling_repos_fixture_is_accepted(tmp_path: Path) -> None:
    spec = load_eval_yaml(_write(tmp_path, "  fixture: e2e_sibling_repos\n"))[0]
    assert spec.fixture == "e2e_sibling_repos"


def test_uv_project_fixture_is_accepted(tmp_path: Path) -> None:
    spec = load_eval_yaml(_write(tmp_path, "  fixture: uv_project\n"))[0]
    assert spec.fixture == "uv_project"


def test_unknown_fixture_is_a_loud_spec_error(tmp_path: Path) -> None:
    with pytest.raises(EvalSpecError, match=r"fixture.*must be one of"):
        load_eval_yaml(_write(tmp_path, "  fixture: not_a_fixture\n"))


def test_on_behalf_ask_cli_stub_is_accepted(tmp_path: Path) -> None:
    spec = load_eval_yaml(_write(tmp_path, "  cli_stubs: [t3@on_behalf_ask]\n"))[0]
    assert spec.cli_stubs == ("t3@on_behalf_ask",)
