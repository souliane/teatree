"""Scenario/fixture `t3 …` invocations must name commands that exist (#3566).

The unreachability class this catches: a scenario or a ``_pass`` fixture cites a
command the CLI does not have, so the scenario grades a trajectory the product
can never take.
"""

import json
from pathlib import Path

from teatree.eval.scenario_reachability import iter_t3_invocations, validate_scenario_reachability

_VALID = {"t3", "t3 teatree", "t3 teatree ticket", "t3 teatree ticket list"}
_GROUPS = {"t3", "t3 teatree", "t3 teatree ticket"}


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    scenarios, fixtures = tmp_path / "scenarios", tmp_path / "fixtures"
    scenarios.mkdir()
    fixtures.mkdir()
    return scenarios, fixtures


class TestInvocationExtraction:
    def test_overlay_placeholder_resolves_to_the_representative_overlay(self) -> None:
        assert iter_t3_invocations("run `t3 <overlay> ticket list`") == ["t3 teatree ticket list"]

    def test_non_t3_text_yields_nothing(self) -> None:
        assert iter_t3_invocations("just prose about tickets") == []


class TestScenarioReachability:
    def test_a_scenario_citing_a_nonexistent_subcommand_is_unreachable(self, tmp_path: Path) -> None:
        scenarios, fixtures = _dirs(tmp_path)
        (scenarios / "s.yaml").write_text("prompt: run t3 teatree ticket frobnicate\n", encoding="utf-8")
        report = validate_scenario_reachability(_VALID, _GROUPS, scenarios_dir=scenarios, fixtures_dir=fixtures)
        assert not report.ok
        assert report.unreachable[0].command == "t3 teatree ticket frobnicate"

    def test_a_scenario_citing_a_real_command_passes(self, tmp_path: Path) -> None:
        scenarios, fixtures = _dirs(tmp_path)
        (scenarios / "s.yaml").write_text("prompt: run t3 teatree ticket list\n", encoding="utf-8")
        assert validate_scenario_reachability(_VALID, _GROUPS, scenarios_dir=scenarios, fixtures_dir=fixtures).ok

    def test_a_fixture_bash_command_is_checked_too(self, tmp_path: Path) -> None:
        # #3566 item 3: the `_pass` fixture citing a nonexistent command is the
        # concrete instance this check exists to catch.
        scenarios, fixtures = _dirs(tmp_path)
        event = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "t3 teatree ticket frobnicate"}}],
            },
        }
        (fixtures / "f.stream.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
        report = validate_scenario_reachability(_VALID, _GROUPS, scenarios_dir=scenarios, fixtures_dir=fixtures)
        assert not report.ok
        assert "f.stream.jsonl" in report.unreachable[0].source

    def test_a_malformed_fixture_line_is_skipped_not_crashed(self, tmp_path: Path) -> None:
        scenarios, fixtures = _dirs(tmp_path)
        (fixtures / "f.stream.jsonl").write_text("{not json\n", encoding="utf-8")
        assert validate_scenario_reachability(_VALID, _GROUPS, scenarios_dir=scenarios, fixtures_dir=fixtures).ok

    def test_the_report_names_the_source_and_the_command(self, tmp_path: Path) -> None:
        scenarios, fixtures = _dirs(tmp_path)
        (scenarios / "s.yaml").write_text("prompt: run t3 teatree ticket frobnicate\n", encoding="utf-8")
        text = validate_scenario_reachability(
            _VALID, _GROUPS, scenarios_dir=scenarios, fixtures_dir=fixtures
        ).render_text()
        assert "s.yaml" in text
        assert "frobnicate" in text
