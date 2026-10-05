"""The static half of the #162 issue-write gate: tracked sources, not tool calls.

The PreToolUse hook only sees a live session. A CI workflow step, a checked-in
script and a skill that TEACHES the wrong command all run outside one — the
ci.yml selection-audit filer was exactly that. These tests pin the two dimensions
that decide whether the scan is usable: it must catch a real invocation wherever
it hides (continuation-wrapped, argv-list, inside a workflow `run:`), and it must
NOT report prose that merely names the bypass, which is most of what the repo's
own gate documentation consists of.
"""

from pathlib import Path

from teatree.hooks.raw_issue_write_sources import (
    WRITE_VERBS,
    in_scope,
    logical_lines,
    scan_paths,
    scan_text,
    write_verbs_in,
)


class TestDetectsAnInvocation:
    def test_a_bare_create_is_a_write(self) -> None:
        assert write_verbs_in("gh issue create --title t --body b") == ["create"]

    def test_a_glab_note_is_a_write(self) -> None:
        assert write_verbs_in("glab issue note 7 --message hi") == ["note"]

    def test_a_continuation_wrapped_invocation_is_joined_before_lexing(self) -> None:
        text = "gh issue create \\\n  --title t \\\n  --body b\n"
        found = scan_text(text, path="scripts/x.sh")
        assert [f.verb for f in found] == ["create"]
        assert found[0].line == 1

    def test_an_invocation_inside_a_workflow_run_block_is_found(self) -> None:
        text = "jobs:\n  a:\n    steps:\n      - run: |\n          gh issue comment 7 --body hi\n"
        assert [f.verb for f in scan_text(text, path=".github/workflows/ci.yml")] == ["comment"]

    def test_a_command_substitution_does_not_hide_the_write(self) -> None:
        assert write_verbs_in("URL=$(gh issue create --title t)") == ["create"]

    def test_an_argv_list_is_found_in_a_python_source(self) -> None:
        text = 'subprocess.run(["gh", "issue", "create", "--title", title], check=True)\n'
        assert [f.verb for f in scan_text(text, path="scripts/file_it.py")] == ["create"]


class TestDoesNotReportAMention:
    def test_prose_naming_the_bypass_in_a_code_span_is_not_a_write(self) -> None:
        line = "Never use `gh issue create` — call the facade instead."
        assert write_verbs_in(line) == []

    def test_a_double_backtick_rst_span_is_not_a_write(self) -> None:
        assert write_verbs_in("surfaces covered include ``gh issue create``, ``glab mr update``") == []

    def test_a_grep_for_the_phrase_is_not_a_write(self) -> None:
        assert write_verbs_in('grep -rn "gh issue comment" hooks/') == []

    def test_a_shell_comment_is_not_a_write(self) -> None:
        assert write_verbs_in("# gh issue create --title t") == []

    def test_a_documented_counter_example_is_skipped(self) -> None:
        text = "gh issue create --title t   # FORBIDDEN — use the facade\n"
        assert scan_text(text, path="skills/x/SKILL.md") == []

    def test_a_python_docstring_naming_the_command_is_not_a_write(self) -> None:
        text = '    """Deny a raw gh issue comment at the Bash boundary."""\n'
        assert scan_text(text, path="hooks/scripts/guard.py") == []

    def test_a_read_verb_is_never_a_write(self) -> None:
        assert write_verbs_in("gh issue list --state open") == []
        assert write_verbs_in("gh issue view 7") == []

    def test_the_sanctioned_body_edit_and_close_are_not_reported(self) -> None:
        """The sweep's own fold path IS `gh issue edit --body-file`; flagging it reports the workflow."""
        assert "edit" not in WRITE_VERBS
        assert "close" not in WRITE_VERBS
        assert write_verbs_in("gh issue edit 7 --body-file merged.md") == []
        assert write_verbs_in('gh issue close 7 --reason "not planned"') == []


class TestScope:
    def test_only_workflow_executable_and_skill_roots_are_scanned(self) -> None:
        assert in_scope(".github/workflows/ci.yml")
        assert in_scope("scripts/ci/x.sh")
        assert in_scope("skills/ship/SKILL.md")
        assert not in_scope("src/teatree/core/issue_hygiene.py")
        assert not in_scope("tests/teatree_hooks/test_raw_issue_write_sources.py")

    def test_recorded_test_timings_are_not_a_source(self) -> None:
        assert not in_scope("dev/.test_durations")

    def test_an_out_of_scope_path_is_not_read(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "x.sh").write_text("gh issue create --title t\n")
        assert scan_paths(["src/x.sh"], root=tmp_path) == []


class TestLogicalLines:
    def test_a_continuation_reports_the_first_line_number(self) -> None:
        joined, rest = list(logical_lines("a \\\nb\nc\n"))
        assert (joined[0], joined[1].split()) == (1, ["a", "b"])
        assert rest == (3, "c")

    def test_a_trailing_continuation_still_yields(self) -> None:
        assert list(logical_lines("a \\\n")) == [(1, "a ")]
