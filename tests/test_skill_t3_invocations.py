"""#550 Tier-1 (gstack): backticked ``t3 …`` in SKILL.md must resolve.

Every such invocation is checked against the live typer command tree. Free,
<2s, no LLM: catches a renamed/removed subcommand cited in skill prose on every
CI run instead of misleading an agent at runtime. Pairs with the
``docs/generated/cli-reference.md`` generator (same introspection);
``command_paths`` is the shared SSOT.

The parse + token-walk logic is PROMOTED into the
:mod:`teatree.eval.skill_command_validity` engine (the same chokepoint the
``t3 eval skill-command-validity`` lane runs) — this test only builds the live
registry and asserts the shipped corpus resolves through that engine, so the
regex and placeholder rules live in exactly one place.
"""

from typing import ClassVar

import pytest

from teatree.cli import app, register_overlay_commands
from teatree.cli_reference import command_groups, command_paths
from teatree.eval.skill_command_validity import DEFAULT_SKILLS_DIR, resolve_command_path, validate_skill_commands


@pytest.fixture(scope="module")
def tree() -> tuple[set[str], set[str]]:
    register_overlay_commands(allowlist={"t3-teatree"})
    return command_paths(app), command_groups(app)


class TestCommandPaths:
    def test_known_core_and_group_paths_present(self, tree: tuple[set[str], set[str]]) -> None:
        paths, groups = tree
        assert "t3 loop tick" in paths
        assert "t3 loop" in paths
        assert "t3 loop" in groups  # loop is a group node
        assert "t3 loop tick" not in groups  # tick is a leaf

    def test_bogus_path_absent(self, tree: tuple[set[str], set[str]]) -> None:
        paths, _ = tree
        assert "t3 loop frobnicate" not in paths
        assert "t3 definitely not a command" not in paths


class TestResolvablePathHelper:
    """The promoted ``resolve_command_path`` engine drives the walk."""

    _valid: ClassVar[set[str]] = {
        "t3",
        "t3 teatree",
        "t3 teatree workspace",
        "t3 teatree workspace ticket",
        "t3 loop",
        "t3 loop tick",
    }
    _groups: ClassVar[set[str]] = {"t3", "t3 teatree", "t3 teatree workspace", "t3 loop"}

    def test_strips_placeholders_and_flags(self) -> None:
        assert (
            resolve_command_path("t3 teatree workspace ticket <url>", self._valid, self._groups)
            == "t3 teatree workspace ticket"
        )
        assert resolve_command_path("t3 loop tick --json", self._valid, self._groups) == "t3 loop tick"

    def test_typoed_subcommand_of_a_group_is_drift(self) -> None:
        # `loop` is a group; `tickk` is not its child -> drift, even though
        # `t3 loop` is itself a valid prefix.
        assert resolve_command_path("t3 loop tickk --json", self._valid, self._groups) is None
        assert resolve_command_path("t3 loop frobnicate", self._valid, self._groups) is None

    def test_arg_after_a_leaf_is_not_drift(self) -> None:
        assert resolve_command_path("t3 loop tick somearg", self._valid, self._groups) == "t3 loop tick"


class TestSkillInvocationsResolve:
    def test_every_backticked_t3_command_in_skills_resolves(self, tree: tuple[set[str], set[str]]) -> None:
        paths, groups = tree
        report = validate_skill_commands(paths, groups, skills_dir=DEFAULT_SKILLS_DIR)
        assert report.ok, (
            "SKILL.md cites t3 command(s) that do not resolve against the live "
            "typer tree (drift — rename/remove or fix the doc):\n" + report.render_text()
        )

    def test_validator_would_catch_a_planted_drifted_command(self, tree: tuple[set[str], set[str]]) -> None:
        paths, groups = tree
        assert resolve_command_path("t3 loop tickk --json", paths, groups) is None
        assert resolve_command_path("t3 workspace ticket <url>", paths, groups) is None
