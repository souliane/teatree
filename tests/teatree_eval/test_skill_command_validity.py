"""skill-command-validity: backticked ``t3 …`` in skill docs must name real commands (#550).

The engine is dependency-inverted — it takes the ``(valid_paths, group_paths)`` registry
as an argument, so these drive it with a small synthetic registry and a tmp skills tree.
"""

from pathlib import Path

from teatree.eval.skill_command_validity import (
    _is_placeholder_only,
    iter_backticked_t3_commands,
    resolve_command_path,
    validate_skill_commands,
)

_VALID = {"t3", "t3 teatree", "t3 teatree ticket", "t3 teatree ticket list"}
_GROUPS = {"t3", "t3 teatree", "t3 teatree ticket"}


class TestIsPlaceholderOnly:
    def test_a_bare_t3_with_no_following_token_names_no_command(self) -> None:
        # `t3` alone has no trailing token, so it is not a placeholder mention either —
        # the empty-token guard returns False.
        assert _is_placeholder_only("t3") is False

    def test_a_leading_ellipsis_is_a_placeholder_mention(self) -> None:
        assert _is_placeholder_only("t3 ...") is True

    def test_an_overlay_template_with_a_placeholder_group_is_a_mention(self) -> None:
        assert _is_placeholder_only("t3 teatree ...") is True

    def test_a_concrete_overlay_command_is_not_a_placeholder(self) -> None:
        assert _is_placeholder_only("t3 teatree ticket list") is False


class TestResolveCommandPath:
    def test_a_real_command_resolves_to_its_deepest_path(self) -> None:
        assert resolve_command_path("t3 teatree ticket list", _VALID, _GROUPS) == "t3 teatree ticket list"

    def test_a_typoed_subcommand_of_a_group_is_drift(self) -> None:
        assert resolve_command_path("t3 teatree ticket frobnicate", _VALID, _GROUPS) is None

    def test_a_positional_arg_after_a_leaf_still_resolves(self) -> None:
        assert resolve_command_path("t3 teatree ticket list 42", _VALID, _GROUPS) == "t3 teatree ticket list"


class TestIterBackticked:
    def test_extracts_backticked_run_commands(self) -> None:
        assert iter_backticked_t3_commands("run `t3 teatree ticket list` now") == ["t3 teatree ticket list"]

    def test_ignores_prose_without_backticks(self) -> None:
        assert iter_backticked_t3_commands("just prose about t3 teatree ticket list") == []


class TestValidateSkillCommands:
    def _skill(self, root: Path, body: str) -> None:
        skill = root / "myskill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(body, encoding="utf-8")

    def test_a_doc_citing_a_missing_command_is_a_violation(self, tmp_path: Path) -> None:
        self._skill(tmp_path, "Run `t3 teatree ticket frobnicate` to do it.\n")
        report = validate_skill_commands(_VALID, _GROUPS, skills_dir=tmp_path)
        assert not report.ok
        assert report.violations[0].command == "t3 teatree ticket frobnicate"
        assert "does not resolve" in report.render_text()

    def test_a_doc_citing_a_real_command_passes(self, tmp_path: Path) -> None:
        self._skill(tmp_path, "Run `t3 teatree ticket list`.\n")
        report = validate_skill_commands(_VALID, _GROUPS, skills_dir=tmp_path)
        assert report.ok
        assert report.checked == 1
