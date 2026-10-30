"""skill-command-validity: backticked ``t3 …`` in repo docs must name real commands (#550).

The engine is dependency-inverted — it takes the ``(valid_paths, group_paths)`` registry
as an argument, so these drive it with a small synthetic registry and a tmp repo root.
"""

from pathlib import Path

import pytest

from teatree.eval.skill_command_validity import (
    _is_placeholder_only,
    iter_backticked_t3_commands,
    iter_fenced_t3_commands,
    resolve_command_path,
    validate_doc_commands,
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


class TestIterFenced:
    def test_reads_fenced_commands_minus_comment_pipe_and_continuation(self) -> None:
        text = "```bash\n$ t3 teatree ticket list | head\nt3 teatree ticket \\\n  list --json  # tail\nls\n```\n"
        assert iter_fenced_t3_commands(text) == ["t3 teatree ticket list", "t3 teatree ticket list --json"]

    def test_ignores_commands_outside_a_fence(self) -> None:
        assert iter_fenced_t3_commands("t3 teatree ticket list\n```bash\nls\n```\nt3 teatree ticket list\n") == []

    @pytest.mark.parametrize(
        ("block", "expected"),
        [
            ("t3 <overlay> ticket start <id>", ["t3 <overlay> ticket start <id>"]),
            ("t3 <overlay> a <b> --x=<c>", ["t3 <overlay> a <b> --x=<c>"]),
            ("$ t3 <overlay> ticket list", ["t3 <overlay> ticket list"]),
            ("FOO=1 t3 loop tick", ["t3 loop tick"]),
            ("FOO=1 BAR=x t3 loop tick", ["t3 loop tick"]),
            ("$ FOO=1 t3 loop tick", ["t3 loop tick"]),
            ("t3 teatree ticket list 2>&1", ["t3 teatree ticket list"]),
            ("t3 teatree ticket list > out.txt", ["t3 teatree ticket list"]),
            ("t3 teatree ticket list >> out.txt", ["t3 teatree ticket list"]),
            ("t3 teatree ticket list &>out.txt", ["t3 teatree ticket list"]),
        ],
    )
    def test_reads_one_whole_command_per_line(self, block: str, expected: list[str]) -> None:
        assert iter_fenced_t3_commands(f"```bash\n{block}\n```\n") == expected

    def test_reads_a_tilde_fence(self) -> None:
        assert iter_fenced_t3_commands("~~~\nt3 teatree ticket list\n~~~\n") == ["t3 teatree ticket list"]

    def test_a_shorter_marker_does_not_close_a_longer_fence(self) -> None:
        text = "````md\n```bash\nt3 teatree ticket list\n```\nt3 teatree ticket start\n````\nt3 teatree outside\n"
        assert iter_fenced_t3_commands(text) == ["t3 teatree ticket list", "t3 teatree ticket start"]

    def test_a_tilde_marker_does_not_close_a_backtick_fence(self) -> None:
        assert iter_fenced_t3_commands("```\n~~~\nt3 teatree ticket list\n```\n") == ["t3 teatree ticket list"]


class TestValidateDocCommands:
    def _skill(self, root: Path, body: str) -> None:
        skill = root / "skills" / "myskill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(body, encoding="utf-8")

    def test_a_doc_citing_a_missing_command_is_a_violation(self, tmp_path: Path) -> None:
        self._skill(tmp_path, "Run `t3 teatree ticket frobnicate` to do it.\n")
        report = validate_doc_commands(_VALID, _GROUPS, repo_root=tmp_path)
        assert not report.ok
        assert report.violations[0].command == "t3 teatree ticket frobnicate"
        assert "does not resolve" in report.render_text()

    def test_a_doc_citing_a_real_command_passes(self, tmp_path: Path) -> None:
        self._skill(tmp_path, "Run `t3 teatree ticket list`.\n")
        report = validate_doc_commands(_VALID, _GROUPS, repo_root=tmp_path)
        assert report.ok
        assert report.checked == 1

    def test_a_fenced_block_citing_a_missing_command_is_a_violation(self, tmp_path: Path) -> None:
        self._skill(tmp_path, "```bash\nt3 teatree ticket list\nt3 teatree ticket frobnicate 7  # stale\n```\n")
        report = validate_doc_commands(_VALID, _GROUPS, repo_root=tmp_path)
        assert [v.command for v in report.violations] == ["t3 teatree ticket frobnicate 7"]
        assert report.checked == 2

    def test_a_fenced_placeholder_command_with_a_missing_subcommand_is_a_violation(self, tmp_path: Path) -> None:
        self._skill(tmp_path, "```bash\nt3 <overlay> ticket frobnicate <id>\n```\n")
        report = validate_doc_commands(_VALID, _GROUPS, repo_root=tmp_path)
        assert [v.command for v in report.violations] == ["t3 <overlay> ticket frobnicate <id>"]

    def test_a_fenced_placeholder_command_with_a_real_subcommand_is_checked_and_passes(self, tmp_path: Path) -> None:
        self._skill(tmp_path, "```bash\nt3 <overlay> ticket list\n```\n")
        report = validate_doc_commands(_VALID, _GROUPS, repo_root=tmp_path)
        assert report.ok
        assert report.checked == 1
