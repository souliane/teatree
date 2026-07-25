"""#550 Tier-1 engine: backticked ``t3 …`` in a repo doc resolves against the registry.

The engine is pure: it takes the live ``(valid_paths, group_paths)`` registry as
an argument (dependency-inverted — no ``teatree.cli`` import) and reports every
backticked invocation that does not resolve. A doc that documents a ``t3``
command which no longer exists is drift (the "no stale references" rule). The
corpus is every human-authored prose doc — the skills tree, ``agents/*.md``,
``BLUEPRINT.md``, ``docs/`` minus the generated pages. A leading ``t3 <overlay>``
(or an illustrative overlay NAME) is substituted with a representative overlay so
the group+sub path behind it is validated too; only a command path that is itself
a placeholder (``t3 …``, ``t3 <overlay> …``) is skipped as a generic mention.
"""

from pathlib import Path

from teatree.eval.skill_command_validity import (
    ALLOWED_NON_RESOLVING,
    DEFAULT_REPO_ROOT,
    expand_alternations,
    iter_backticked_t3_commands,
    resolve_command_path,
    validate_doc_commands,
)

_VALID: set[str] = {
    "t3",
    "t3 teatree",
    "t3 teatree workspace",
    "t3 teatree workspace ticket",
    "t3 loop",
    "t3 loop tick",
    "t3 loop enable",
    "t3 loop disable",
    "t3 eval",
    "t3 eval coverage",
}
_GROUPS: set[str] = {"t3", "t3 teatree", "t3 teatree workspace", "t3 loop", "t3 eval"}


def _skill(repo_root: Path, name: str, body: str) -> Path:
    d = repo_root / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    md.write_text(f"---\nname: {name}\n---\n{body}\n", encoding="utf-8")
    return md


def _doc(repo_root: Path, rel: str, body: str) -> Path:
    md = repo_root / rel
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(body + "\n", encoding="utf-8")
    return md


class TestResolveCommandPath:
    def test_strips_placeholders_and_flags(self) -> None:
        assert (
            resolve_command_path("t3 teatree workspace ticket <url>", _VALID, _GROUPS) == "t3 teatree workspace ticket"
        )
        assert resolve_command_path("t3 loop tick --json", _VALID, _GROUPS) == "t3 loop tick"

    def test_typoed_subcommand_of_a_group_is_drift(self) -> None:
        assert resolve_command_path("t3 loop tickk --json", _VALID, _GROUPS) is None
        assert resolve_command_path("t3 loop frobnicate", _VALID, _GROUPS) is None

    def test_bogus_top_level_command_is_drift(self) -> None:
        assert resolve_command_path("t3 frobnicate", _VALID, _GROUPS) is None

    def test_arg_after_a_leaf_is_not_drift(self) -> None:
        assert resolve_command_path("t3 loop tick somearg", _VALID, _GROUPS) == "t3 loop tick"

    def test_first_token_placeholder_resolves_to_the_bare_root(self) -> None:
        # Called DIRECTLY, the resolver treats any first-token placeholder as the
        # halt point, so the walk stops at the root group `t3`. `validate_doc_commands`
        # substitutes a leading `<overlay>` with a concrete overlay BEFORE calling
        # the resolver, so the overlay form is validated there — this direct call
        # exercises only the token-walker's own placeholder-halt contract.
        assert resolve_command_path("t3 <overlay> workspace ticket", _VALID, _GROUPS) == "t3"
        assert resolve_command_path("t3 ...", _VALID, _GROUPS) == "t3"
        assert resolve_command_path("t3 …", _VALID, _GROUPS) == "t3"


class TestIterBacktickedCommands:
    def test_extracts_only_backticked_t3_runs(self) -> None:
        text = "run `t3 loop tick` then `t3 eval coverage`. Not t3 loop tick (no backticks)."
        assert iter_backticked_t3_commands(text) == ["t3 loop tick", "t3 eval coverage"]

    def test_ignores_non_t3_backticks(self) -> None:
        assert iter_backticked_t3_commands("use `git status` and `t3 eval`") == ["t3 eval"]


class TestExpandAlternations:
    def test_pipe_and_slash_enumerations_become_one_variant_each(self) -> None:
        assert expand_alternations("t3 loop enable/disable") == ["t3 loop enable", "t3 loop disable"]
        assert expand_alternations("t3 prompts list|render") == ["t3 prompts list", "t3 prompts render"]

    def test_a_plain_command_is_its_own_only_variant(self) -> None:
        assert expand_alternations("t3 loop tick --json") == ["t3 loop tick --json"]

    def test_a_shell_line_continuation_is_dropped(self) -> None:
        assert expand_alternations("t3 loop tick\\") == ["t3 loop tick"]


class TestValidateDocCommands:
    def test_bogus_command_in_a_skill_is_a_violation(self, tmp_path: Path) -> None:
        _skill(tmp_path, "bad", "Run `t3 frobnicate` to do the thing.")
        report = validate_doc_commands(_VALID, _GROUPS, repo_root=tmp_path)
        assert not report.ok
        assert len(report.violations) == 1
        violation = report.violations[0]
        assert violation.doc == "skills/bad/SKILL.md"
        assert violation.command == "t3 frobnicate"

    def test_real_commands_in_a_skill_pass(self, tmp_path: Path) -> None:
        _skill(tmp_path, "good", "Run `t3 loop tick` and `t3 eval coverage`.")
        report = validate_doc_commands(_VALID, _GROUPS, repo_root=tmp_path)
        assert report.ok
        assert report.violations == ()
        assert report.checked >= 2

    def test_placeholder_and_overlay_forms_do_not_trip_the_lane(self, tmp_path: Path) -> None:
        _skill(tmp_path, "doc", "Run `t3 <overlay> workspace ticket <url>` or just `t3 ...`.")
        report = validate_doc_commands(_VALID, _GROUPS, repo_root=tmp_path)
        assert report.ok

    def test_overlay_placeholder_command_path_is_validated(self, tmp_path: Path) -> None:
        # `t3 <overlay> <group> <sub>` resolves the `<overlay>` placeholder to the
        # representative overlay, then validates the group+sub path against the
        # registry. A subcommand absent from the registry is drift — the leading
        # placeholder no longer short-circuits the check.
        valid = {"t3", "t3 teatree", "t3 teatree lifecycle"}
        groups = {"t3", "t3 teatree", "t3 teatree lifecycle"}
        _skill(tmp_path, "attest", "Attest via `t3 <overlay> lifecycle record-e2e-run <id>`.")
        report = validate_doc_commands(valid, groups, repo_root=tmp_path)
        assert not report.ok
        assert report.violations[0].command == "t3 <overlay> lifecycle record-e2e-run <id>"

    def test_overlay_placeholder_real_command_validates_ok(self, tmp_path: Path) -> None:
        valid = {"t3", "t3 teatree", "t3 teatree ticket", "t3 teatree ticket list"}
        groups = {"t3", "t3 teatree", "t3 teatree ticket"}
        _skill(tmp_path, "list", "Enumerate with `t3 <overlay> ticket list`.")
        report = validate_doc_commands(valid, groups, repo_root=tmp_path)
        assert report.ok

    def test_an_illustrative_overlay_name_is_the_same_slot_as_the_placeholder(self, tmp_path: Path) -> None:
        valid = {"t3", "t3 teatree", "t3 teatree ticket", "t3 teatree ticket list"}
        groups = {"t3", "t3 teatree", "t3 teatree ticket"}
        _doc(tmp_path, "agents/role.md", "Prefix every dispatch with `t3 acme`, e.g. `t3 acme ticket list`.")
        assert validate_doc_commands(valid, groups, repo_root=tmp_path).ok

    def test_a_wrong_subcommand_behind_an_illustrative_overlay_name_is_still_drift(self, tmp_path: Path) -> None:
        valid = {"t3", "t3 teatree", "t3 teatree ticket", "t3 teatree ticket list"}
        groups = {"t3", "t3 teatree", "t3 teatree ticket"}
        _doc(tmp_path, "agents/role.md", "Run `t3 acme ticket update <id>`.")
        report = validate_doc_commands(valid, groups, repo_root=tmp_path)
        assert not report.ok
        assert report.violations[0].command == "t3 acme ticket update <id>"

    def test_a_placeholder_group_slot_names_no_concrete_command(self, tmp_path: Path) -> None:
        # `t3 <overlay> <group> <sub>` is a doc TEMPLATE: substituting the overlay
        # leaves a group placeholder, so there is no concrete command path to check.
        valid = {"t3", "t3 teatree ticket list"}
        groups = {"t3", "t3 teatree", "t3 teatree ticket"}
        _skill(tmp_path, "shape", "Every overlay command is `t3 <overlay> <group> <sub>`.")
        report = validate_doc_commands(valid, groups, repo_root=tmp_path)
        assert report.ok
        assert report.checked == 0

    def test_overlay_placeholder_in_arg_position_is_still_skipped(self, tmp_path: Path) -> None:
        valid = {"t3", "t3 teatree", "t3 teatree ticket", "t3 teatree ticket list"}
        groups = {"t3", "t3 teatree", "t3 teatree ticket"}
        _skill(tmp_path, "generic", "Run `t3 <overlay> ...` or `t3 <overlay> ticket list <id>`.")
        report = validate_doc_commands(valid, groups, repo_root=tmp_path)
        assert report.ok

    def test_typoed_subcommand_is_a_violation(self, tmp_path: Path) -> None:
        _skill(tmp_path, "typo", "Run `t3 loop tickk` to tick.")
        report = validate_doc_commands(_VALID, _GROUPS, repo_root=tmp_path)
        assert not report.ok
        assert report.violations[0].command == "t3 loop tickk"

    def test_walks_nested_md_under_a_skill_dir(self, tmp_path: Path) -> None:
        _doc(tmp_path, "skills/deep/references/x.md", "Stale `t3 frobnicate` reference.")
        _doc(tmp_path, "skills/deep/SKILL.md", "---\nname: deep\n---\nok")
        report = validate_doc_commands(_VALID, _GROUPS, repo_root=tmp_path)
        assert not report.ok
        assert report.violations[0].command == "t3 frobnicate"

    def test_render_text_names_each_violation(self, tmp_path: Path) -> None:
        _skill(tmp_path, "bad", "Run `t3 frobnicate`.")
        rendered = validate_doc_commands(_VALID, _GROUPS, repo_root=tmp_path).render_text()
        assert "skills/bad/SKILL.md" in rendered
        assert "t3 frobnicate" in rendered


class TestWidenedCorpus:
    """The corpus reaches beyond ``skills/`` — that widening is what caught #3745."""

    def test_a_stale_command_in_blueprint_is_a_violation(self, tmp_path: Path) -> None:
        _doc(tmp_path, "BLUEPRINT.md", "Un-darken it with `t3 loops enable outer_loop`.")
        report = validate_doc_commands(_VALID, _GROUPS, repo_root=tmp_path)
        assert not report.ok
        assert report.violations[0].doc == "BLUEPRINT.md"

    def test_a_stale_command_in_an_agent_brief_is_a_violation(self, tmp_path: Path) -> None:
        _doc(tmp_path, "agents/orchestrator.md", "Run `t3 <overlay> ticket update <ID> --extra k=v`.")
        report = validate_doc_commands(_VALID, _GROUPS, repo_root=tmp_path)
        assert not report.ok
        assert report.violations[0].doc == "agents/orchestrator.md"

    def test_a_stale_command_under_docs_is_a_violation(self, tmp_path: Path) -> None:
        _doc(tmp_path, "docs/blueprint/loop-topology.md", "Merge with `t3 ticket merge`.")
        report = validate_doc_commands(_VALID, _GROUPS, repo_root=tmp_path)
        assert not report.ok
        assert report.violations[0].doc == "docs/blueprint/loop-topology.md"

    def test_generated_docs_are_out_of_corpus(self, tmp_path: Path) -> None:
        # Rendered FROM the command tree: its box-drawn help wraps one command
        # across lines, so a fragment is a rendering artifact, not doc drift.
        _doc(tmp_path, "docs/generated/cli-reference.md", "`t3 loop            │\n│ claim-next`")
        assert validate_doc_commands(_VALID, _GROUPS, repo_root=tmp_path).ok

    def test_an_entry_point_spec_is_not_an_invocation(self, tmp_path: Path) -> None:
        _doc(tmp_path, "BLUEPRINT.md", "The console script is `t3 = t3_bootstrap:main`.")
        report = validate_doc_commands(_VALID, _GROUPS, repo_root=tmp_path)
        assert report.ok
        assert report.checked == 0

    def test_each_alternative_of_an_enumeration_is_walked(self, tmp_path: Path) -> None:
        _doc(tmp_path, "BLUEPRINT.md", "Control with `t3 loop enable/disable` or `t3 loop enable/frobnicate`.")
        report = validate_doc_commands(_VALID, _GROUPS, repo_root=tmp_path)
        assert not report.ok
        assert [v.command for v in report.violations] == ["t3 loop enable/frobnicate"]


class TestAllowlist:
    def test_an_allowlisted_citation_is_not_a_violation(self, tmp_path: Path) -> None:
        entry = next(iter(ALLOWED_NON_RESOLVING))
        _doc(tmp_path, "BLUEPRINT.md", f"There is no `{entry}`.")
        assert validate_doc_commands(_VALID, _GROUPS, repo_root=tmp_path).ok

    def test_every_entry_carries_a_justification(self) -> None:
        assert all(reason.strip() for reason in ALLOWED_NON_RESOLVING.values())


class TestShippedDocsResolve:
    """The engine walks the real repo tree without raising.

    The live-registry assertion is the lane test (which builds the registry from
    the typer app). Here we only assert the engine is callable over the real repo
    root with an empty registry — every non-placeholder is then a violation,
    proving the walker actually reaches the shipped files.
    """

    def test_engine_runs_over_the_shipped_repo_root(self) -> None:
        report = validate_doc_commands(set(), set(), repo_root=DEFAULT_REPO_ROOT)
        assert report.checked > 0
        assert not report.ok
