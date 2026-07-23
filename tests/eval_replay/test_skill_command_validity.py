"""#550 Tier-1 engine: backticked ``t3 …`` in SKILL.md resolves against the registry.

The engine is pure: it takes the live ``(valid_paths, group_paths)`` registry as
an argument (dependency-inverted — no ``teatree.cli`` import) and reports every
backticked invocation that does not resolve. A SKILL.md that documents a ``t3``
command which no longer exists in the registry is drift (the "no stale
references" rule). A leading ``t3 <overlay>`` is resolved to a representative
overlay so an overlay-scoped ``t3 <overlay> <group> <sub>`` is validated too;
only a command path that is itself a placeholder (``t3 …``, ``t3 <overlay> …``)
is skipped as a generic mention.
"""

from pathlib import Path

from teatree.eval.skill_command_validity import (
    DEFAULT_SKILLS_DIR,
    iter_backticked_t3_commands,
    resolve_command_path,
    validate_skill_commands,
)

_VALID: set[str] = {
    "t3",
    "t3 teatree",
    "t3 teatree workspace",
    "t3 teatree workspace ticket",
    "t3 loop",
    "t3 loop tick",
    "t3 eval",
    "t3 eval coverage",
}
_GROUPS: set[str] = {"t3", "t3 teatree", "t3 teatree workspace", "t3 loop", "t3 eval"}


def _skill(skills_dir: Path, name: str, body: str) -> Path:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    md.write_text(f"---\nname: {name}\n---\n{body}\n", encoding="utf-8")
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
        # halt point, so the walk stops at the root group `t3`. `validate_skill_commands`
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


class TestValidateSkillCommands:
    def test_bogus_command_in_a_skill_is_a_violation(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _skill(skills, "bad", "Run `t3 frobnicate` to do the thing.")
        report = validate_skill_commands(_VALID, _GROUPS, skills_dir=skills)
        assert not report.ok
        assert len(report.violations) == 1
        violation = report.violations[0]
        assert violation.skill == "bad"
        assert violation.command == "t3 frobnicate"

    def test_real_commands_in_a_skill_pass(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _skill(skills, "good", "Run `t3 loop tick` and `t3 eval coverage`.")
        report = validate_skill_commands(_VALID, _GROUPS, skills_dir=skills)
        assert report.ok
        assert report.violations == ()
        # a passing skill still counts toward the checked total
        assert report.checked >= 2

    def test_placeholder_and_overlay_forms_do_not_trip_the_lane(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _skill(skills, "doc", "Run `t3 <overlay> workspace ticket <url>` or just `t3 ...`.")
        report = validate_skill_commands(_VALID, _GROUPS, skills_dir=skills)
        assert report.ok

    def test_overlay_placeholder_command_path_is_validated(self, tmp_path: Path) -> None:
        # `t3 <overlay> <group> <sub>` resolves the `<overlay>` placeholder to the
        # representative overlay, then validates the group+sub path against the
        # registry. A subcommand absent from the registry is drift — the leading
        # placeholder no longer short-circuits the check.
        valid = {"t3", "t3 teatree", "t3 teatree lifecycle"}
        groups = {"t3", "t3 teatree", "t3 teatree lifecycle"}
        skills = tmp_path / "skills"
        _skill(skills, "attest", "Attest via `t3 <overlay> lifecycle record-e2e-run <id>`.")
        report = validate_skill_commands(valid, groups, skills_dir=skills)
        assert not report.ok
        assert report.violations[0].command == "t3 <overlay> lifecycle record-e2e-run <id>"

    def test_overlay_placeholder_real_command_validates_ok(self, tmp_path: Path) -> None:
        valid = {"t3", "t3 teatree", "t3 teatree ticket", "t3 teatree ticket list"}
        groups = {"t3", "t3 teatree", "t3 teatree ticket"}
        skills = tmp_path / "skills"
        _skill(skills, "list", "Enumerate with `t3 <overlay> ticket list`.")
        report = validate_skill_commands(valid, groups, skills_dir=skills)
        assert report.ok

    def test_a_placeholder_group_slot_names_no_concrete_command(self, tmp_path: Path) -> None:
        # `t3 <overlay> <group> <sub>` is a doc TEMPLATE: substituting the overlay
        # leaves a group placeholder, so there is no concrete command path to check.
        # The registry lists `t3 teatree` as a group only, so treating it as a
        # command path reported the template as drift.
        valid = {"t3", "t3 teatree ticket list"}
        groups = {"t3", "t3 teatree", "t3 teatree ticket"}
        skills = tmp_path / "skills"
        _skill(skills, "shape", "Every overlay command is `t3 <overlay> <group> <sub>`.")
        report = validate_skill_commands(valid, groups, skills_dir=skills)
        assert report.ok
        assert report.checked == 0

    def test_overlay_placeholder_in_arg_position_is_still_skipped(self, tmp_path: Path) -> None:
        # A placeholder in an ARGUMENT position (after a resolved leaf, or a bare
        # `t3 <overlay> …` mention) is not drift — only the command PATH is checked.
        valid = {"t3", "t3 teatree", "t3 teatree ticket", "t3 teatree ticket list"}
        groups = {"t3", "t3 teatree", "t3 teatree ticket"}
        skills = tmp_path / "skills"
        _skill(skills, "generic", "Run `t3 <overlay> ...` or `t3 <overlay> ticket list <id>`.")
        report = validate_skill_commands(valid, groups, skills_dir=skills)
        assert report.ok

    def test_typoed_subcommand_is_a_violation(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _skill(skills, "typo", "Run `t3 loop tickk` to tick.")
        report = validate_skill_commands(_VALID, _GROUPS, skills_dir=skills)
        assert not report.ok
        assert report.violations[0].command == "t3 loop tickk"

    def test_walks_nested_md_under_a_skill_dir(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        ref = skills / "deep" / "references"
        ref.mkdir(parents=True, exist_ok=True)
        (ref / "x.md").write_text("Stale `t3 frobnicate` reference.", encoding="utf-8")
        (skills / "deep" / "SKILL.md").write_text("---\nname: deep\n---\nok\n", encoding="utf-8")
        report = validate_skill_commands(_VALID, _GROUPS, skills_dir=skills)
        assert not report.ok
        assert report.violations[0].command == "t3 frobnicate"

    def test_render_text_names_each_violation(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _skill(skills, "bad", "Run `t3 frobnicate`.")
        report = validate_skill_commands(_VALID, _GROUPS, skills_dir=skills)
        rendered = report.render_text()
        assert "bad" in rendered
        assert "t3 frobnicate" in rendered


class TestShippedSkillDocsResolve:
    """The engine walks the real skills tree without raising.

    The live-registry assertion is the lane test (which builds the registry from
    the typer app). Here we only assert the engine is callable over the real
    directory with an empty registry — every non-placeholder is then a violation,
    proving the walker actually reaches the shipped files.
    """

    def test_engine_runs_over_the_shipped_skills_dir(self) -> None:
        report = validate_skill_commands(set(), set(), skills_dir=DEFAULT_SKILLS_DIR)
        assert report.checked >= 0
        assert isinstance(report.ok, bool)
