"""#550 PR-A (gstack Tier-1): backticked ``t3 …`` in SKILL.md must resolve.

Every such invocation is checked against the live typer command tree.
Free, <2s, no LLM: catches a renamed/removed subcommand cited in skill
prose on every CI run instead of misleading an agent at runtime. Pairs
with the existing ``docs/generated/cli-reference.md`` generator (same
introspection); ``command_paths`` is the shared SSOT.
"""

import re
from pathlib import Path
from typing import ClassVar

import pytest

from teatree.cli import app, register_overlay_commands
from teatree.cli_reference import command_groups, command_paths

_SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"

# A backticked ``t3 …`` run command inside a SKILL.md. Stops at the
# closing backtick; we normalize the captured words afterwards.
_T3_IN_BACKTICKS = re.compile(r"`(t3 [^`]+)`")

# Tokens that terminate the command path: an option/flag, a shell
# placeholder, a redirect/pipe, an ASCII or unicode ellipsis ("`t3 ...`" /
# "`t3 …`" = a generic mention of the CLI, not a specific command), or an
# argument value.
_PLACEHOLDER = re.compile(r"^(\.\.\.|…|<.*>|\$.*|--.*|-[A-Za-z]|\{.*\}|\|.*|>.*|\".*|'.*)$")


def _resolvable_path(raw: str, valid: set[str], groups: set[str]) -> str | None:
    """Token-walk a backticked invocation against the command tree.

    Descends token by token while each extends a valid path. Drift
    (returns ``None``) iff the deepest matched node is a **group** and
    the next non-placeholder token does NOT extend it to a valid child
    path — i.e. a typo'd/removed subcommand. A token after a **leaf**
    (or a placeholder/flag anywhere) is a normal argument, not drift.
    ``t3`` itself is the root group.
    """
    toks = raw.split()
    if not toks or toks[0] != "t3":
        return None
    matched = "t3"
    for tok in toks[1:]:
        if _PLACEHOLDER.match(tok):
            break  # args/flags begin — matched node stands
        nxt = f"{matched} {tok}"
        if nxt in valid:
            matched = nxt
            continue
        # `tok` does not extend `matched`. If `matched` is a group, the
        # next word was supposed to be a subcommand → drift. If it is a
        # leaf, `tok` is a positional argument → stop, matched is fine.
        if matched in groups:
            return None
        break
    return matched if matched in valid else None


def _iter_skill_invocations() -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for skill_md in sorted(_SKILLS_DIR.glob("*/**/*.md")):
        text = skill_md.read_text(encoding="utf-8")
        found.extend((skill_md, m.group(1).strip()) for m in _T3_IN_BACKTICKS.finditer(text))
    return found


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
            _resolvable_path("t3 teatree workspace ticket <url>", self._valid, self._groups)
            == "t3 teatree workspace ticket"
        )
        assert _resolvable_path("t3 loop tick --json", self._valid, self._groups) == "t3 loop tick"

    def test_typoed_subcommand_of_a_group_is_drift(self) -> None:
        # `loop` is a group; `tickk` is not its child -> drift, even
        # though `t3 loop` is itself a valid prefix.
        assert _resolvable_path("t3 loop tickk --json", self._valid, self._groups) is None
        assert _resolvable_path("t3 loop frobnicate", self._valid, self._groups) is None

    def test_arg_after_a_leaf_is_not_drift(self) -> None:
        assert _resolvable_path("t3 loop tick somearg", self._valid, self._groups) == "t3 loop tick"


class TestSkillInvocationsResolve:
    def test_every_backticked_t3_command_in_skills_resolves(self, tree: tuple[set[str], set[str]]) -> None:
        paths, groups = tree
        unresolved: list[str] = []
        for skill_md, raw in _iter_skill_invocations():
            if _resolvable_path(raw, paths, groups) is None:
                unresolved.append(f"{skill_md.parent.name}/SKILL.md: `{raw}`")
        assert not unresolved, (
            "SKILL.md cites t3 command(s) that do not resolve against the "
            "live typer tree (drift — rename/remove or fix the doc):\n" + "\n".join(unresolved)
        )

    def test_validator_would_catch_a_planted_drifted_command(self, tree: tuple[set[str], set[str]]) -> None:
        paths, groups = tree
        assert _resolvable_path("t3 loop tickk --json", paths, groups) is None
        assert _resolvable_path("t3 workspace ticket <url>", paths, groups) is None
