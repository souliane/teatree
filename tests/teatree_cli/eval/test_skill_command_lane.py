"""#550 Tier-1 lane: ``t3 eval skill-command-validity`` over the live registry.

The lane builds the live CLI registry from the typer app (``command_paths`` /
``command_groups``) and validates every backticked ``t3 …`` in the shipped skill
docs against it. A SKILL.md that cites a renamed/removed ``t3`` command FAILs the
lane (the "no stale references" rule). The lane is wired into ``t3 eval all``.
"""

from pathlib import Path

from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.eval.skill_command_lane import (
    build_command_registry,
    skill_command_validity_lane,
    validate_shipped_skill_commands,
)


class TestLiveRegistry:
    def test_registry_contains_known_paths_and_groups(self) -> None:
        valid, groups = build_command_registry()
        assert "t3 eval coverage" in valid
        assert "t3 loop" in groups
        assert "t3 loop tick" in valid
        assert "t3 loop tick" not in groups  # tick is a leaf

    def test_bogus_command_absent_from_registry(self) -> None:
        valid, _ = build_command_registry()
        assert "t3 frobnicate" not in valid


class TestShippedCorpus:
    def test_shipped_skill_docs_all_resolve(self) -> None:
        report = validate_shipped_skill_commands()
        assert report.ok, report.render_text()
        assert report.checked > 0  # the lane is not vacuous — it checked real commands

    def test_lane_catches_a_planted_stale_command(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        d = skills / "stale"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: stale\n---\nRun `t3 frobnicate`.\n", encoding="utf-8")
        report = validate_shipped_skill_commands(skills_dir=skills)
        assert not report.ok
        assert report.violations[0].command == "t3 frobnicate"


class TestLaneResult:
    def test_clean_corpus_is_a_passing_free_lane(self) -> None:
        lane = skill_command_validity_lane(validate_shipped_skill_commands())
        assert lane.name == "skill-command-validity"
        assert lane.cost == "free"
        assert lane.passed is True
        assert lane.skipped is False

    def test_violation_makes_the_lane_fail(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        d = skills / "stale"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: stale\n---\n`t3 frobnicate`\n", encoding="utf-8")
        lane = skill_command_validity_lane(validate_shipped_skill_commands(skills_dir=skills))
        assert lane.passed is False


class TestStandaloneCommand:
    def test_skill_command_validity_subcommand_passes_on_clean_corpus(self) -> None:
        result = CliRunner().invoke(app, ["eval", "skill-command-validity"])
        assert result.exit_code == 0, result.output
        assert "skill-command-validity" in result.output or "resolve" in result.output

    def test_json_format_is_accepted(self) -> None:
        result = CliRunner().invoke(app, ["eval", "skill-command-validity", "--format", "json"])
        assert result.exit_code == 0, result.output
        assert '"ok"' in result.output
