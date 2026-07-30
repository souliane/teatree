"""#550 Tier-1 (gstack): backticked ``t3 …`` in a repo doc must resolve.

Every such invocation is checked against the live typer command tree. Free,
<2s, no LLM: catches a renamed/removed subcommand cited in prose on every
CI run instead of misleading an agent at runtime. Pairs with the
``docs/generated/cli-reference.md`` generator (same introspection);
``command_paths`` is the shared SSOT.

The corpus is every human-authored doc (:data:`DOC_GLOBS`) — the skills tree,
the ``agents/*.md`` role briefs, ``BLUEPRINT.md``, and ``docs/`` minus the
generated pages. Widening it past ``skills/`` is what surfaced the #3745 batch:
a BLUEPRINT operator instruction, an agent brief, and several ``docs/`` pages all
cited ``t3`` commands that do not exist.

The parse + token-walk logic is PROMOTED into the
:mod:`teatree.eval.skill_command_validity` engine (the same chokepoint the
``t3 eval skill-command-validity`` lane runs) — this test only builds the live
registry and asserts the shipped corpus resolves through that engine, so the
regex and placeholder rules live in exactly one place.
"""

from pathlib import Path
from typing import ClassVar

import pytest

from teatree.cli import app, register_overlay_commands
from teatree.cli_reference import command_groups, command_paths
from teatree.eval.skill_command_validity import (
    ALLOWED_NON_RESOLVING,
    DEFAULT_REPO_ROOT,
    citation_resolves,
    resolve_command_path,
    validate_doc_commands,
)


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


class TestDocInvocationsResolve:
    def test_every_backticked_t3_command_in_the_repo_docs_resolves(self, tree: tuple[set[str], set[str]]) -> None:
        paths, groups = tree
        report = validate_doc_commands(paths, groups, repo_root=DEFAULT_REPO_ROOT)
        assert report.ok, (
            "repo doc(s) cite t3 command(s) that do not resolve against the live "
            "typer tree (drift — rename/remove or fix the doc):\n" + report.render_text()
        )
        assert report.checked > 0

    def test_validator_would_catch_a_planted_drifted_command(self, tree: tuple[set[str], set[str]]) -> None:
        paths, groups = tree
        assert resolve_command_path("t3 loop tickk --json", paths, groups) is None
        assert resolve_command_path("t3 workspace ticket <url>", paths, groups) is None


class TestGateIsAntiVacuousAgainstThePreFixDocs:
    """Each #3745 citation must go RED — a gate green against them guards nothing.

    Every entry is the exact string the doc carried before this change. They are
    replayed against the live registry, so the check keeps its teeth even after
    the docs are fixed and the shipped corpus is clean.
    """

    _PRE_FIX_CITATIONS: ClassVar[tuple[str, ...]] = (
        "t3 loops enable outer_loop",
        "t3 <overlay> ticket update <ID> --extra auto_started=true",
        "t3 <overlay> start-ticket",
        "t3 <overlay> outer status|propose|history|tick",
        "t3 workspace doctor",
        "t3 workspace clean-all",
        "t3 availability away|autonomous-away|present|auto [--until ISO8601]",
        "t3 questions answer <id> <text>",
        "t3 pr create",
        "t3 ticket clear/merge",
        "t3 e2e external --repo <name>",
        "t3 <overlay> directive capture",
    )

    @pytest.mark.parametrize("citation", _PRE_FIX_CITATIONS)
    def test_pre_fix_citation_does_not_resolve(self, citation: str, tree: tuple[set[str], set[str]]) -> None:
        paths, groups = tree
        assert citation_resolves(citation, paths, groups) is False, (
            f"`{citation}` resolves, so the gate could not have caught it — the drift fix is unguarded"
        )

    @pytest.mark.parametrize("citation", _PRE_FIX_CITATIONS)
    def test_pre_fix_citation_is_gone_from_the_shipped_docs(self, citation: str) -> None:
        planted = [
            md.relative_to(DEFAULT_REPO_ROOT).as_posix()
            for md in sorted(DEFAULT_REPO_ROOT.glob("**/*.md"))
            if _is_gated_doc(md) and f"`{citation}`" in md.read_text(encoding="utf-8")
        ]
        assert not planted, f"`{citation}` is still cited in {planted}"


def _is_gated_doc(md: Path) -> bool:
    rel = md.relative_to(DEFAULT_REPO_ROOT).as_posix()
    return rel.startswith(("BLUEPRINT.md", "agents/", "docs/", "skills/")) and not rel.startswith("docs/generated/")


class TestAllowlistIsLive:
    def test_no_allowlist_entry_has_started_resolving(self, tree: tuple[set[str], set[str]]) -> None:
        # Anti-rot: an exemption that now resolves is stale and must be deleted,
        # or it silently widens into cover for a future real drift.
        paths, groups = tree
        stale = [raw for raw in ALLOWED_NON_RESOLVING if citation_resolves(raw, paths, groups) is not False]
        assert not stale, f"allowlist entries now resolve and should be removed: {stale}"
