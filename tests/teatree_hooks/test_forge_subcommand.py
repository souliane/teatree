"""One traversal answers "is this forge subcommand at a command position" for every gate.

The MERGE and CREATE gates asked the same question with two copies of the same walk, so a form
one recognised (a wrapper prefix, a nested command substitution, a path-qualified program word)
could silently go unrecognised by the other. These tests pin the generic contract directly, on a
subcommand table neither gate uses, so a regression cannot hide behind either caller's own suite.
"""

import pytest

from teatree.hooks.forge_subcommand import (
    ApiCall,
    command_substitution_bodies,
    effective_method_is_write,
    forge_api_calls,
    invokes_forge_subcommand,
    program_words,
    segment_word_lists,
    strip_heredoc_bodies,
)

_SUBWORDS: dict[str, tuple[str, ...]] = {"gh": ("repo", "delete"), "glab": ("project", "archive")}


class TestInvokesForgeSubcommand:
    @pytest.mark.parametrize(
        "command",
        [
            "gh repo delete o/r",
            "glab project archive 9",
            "TOKEN=x gh repo delete o/r",
            "env FOO=bar /usr/bin/gh repo delete o/r",
            "xargs glab project archive",
            "echo hi && gh repo delete o/r",
            "echo $(echo $(gh repo delete o/r))",
            "result=`glab project archive 9`",
            "{ gh repo delete o/r; }",
            "if true; then glab project archive 9; fi",
            "env -i gh repo delete o/r",
            "env -u HOME gh repo delete o/r",
            "time -p glab project archive 9",
            "command -- gh repo delete o/r",
            "xargs -n 1 gh repo delete",
            "env -i time -p gh repo delete o/r",
        ],
    )
    def test_any_plausible_invocation_fires(self, command: str) -> None:
        assert invokes_forge_subcommand(command, _SUBWORDS) is True

    @pytest.mark.parametrize(
        "command",
        [
            "",
            "gh repo view o/r",
            "glab project list",
            "echo 'gh repo delete o/r'",
            'grep "glab project archive" notes.md',
            "ls  # gh repo delete o/r",
            "cat >> note.md <<EOF\ngh repo delete o/r\nEOF",
            "TOKEN=x VERBOSE=1",
            "command env",
            "echo '$(gh repo delete o/r)'",
            "echo 'run `glab project archive 9`'",
        ],
    )
    def test_non_invocation_text_does_not_fire(self, command: str) -> None:
        assert invokes_forge_subcommand(command, _SUBWORDS) is False

    def test_a_subcommand_table_the_command_does_not_match_never_fires(self) -> None:
        assert invokes_forge_subcommand("gh pr merge 5", _SUBWORDS) is False


class TestEffectiveMethodIsWrite:
    @pytest.mark.parametrize(
        "command",
        [
            "gh api x -X POST",
            "gh api x --method=PUT",
            "gh api x -XPATCH",
            "gh api x -f a=b",
            "gh api x -fa=b",
            "gh api x -X GET -X PUT",
        ],
    )
    def test_a_write_or_an_implied_write_reads_as_a_write(self, command: str) -> None:
        assert effective_method_is_write(command) is True

    @pytest.mark.parametrize("command", ["gh api x", "gh api x -X GET", "gh api x -X POST -X GET", ""])
    def test_a_get_and_a_bare_read_are_not_writes(self, command: str) -> None:
        assert effective_method_is_write(command) is False


class TestForgeApiCalls:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("gh api repos/o/r/pulls -ftitle=x", ApiCall("repos/o/r/pulls", None, has_body=True)),
            ("glab api projects/9/merge_requests -XPOST", ApiCall("projects/9/merge_requests", "POST", has_body=False)),
            ("gh api -H 'Accept: x' repos/o/r/pulls --method=GET", ApiCall("repos/o/r/pulls", "GET", has_body=False)),
            ("env -i gh api repos/o/r/pulls -f title=x", ApiCall("repos/o/r/pulls", None, has_body=True)),
            (
                "echo $(glab api projects/9/merge_requests -F title=x)",
                ApiCall("projects/9/merge_requests", None, has_body=True),
            ),
        ],
    )
    def test_an_executed_call_is_read_from_its_argv(self, command: str, expected: ApiCall) -> None:
        assert forge_api_calls(command) == [expected]

    @pytest.mark.parametrize(
        "command",
        [
            "echo 'gh api repos/o/r/pulls -f title=x'",
            "cat <<EOF\ngh api repos/o/r/pulls -f title=x\nEOF",
            "ls  # gh api repos/o/r/pulls -f title=x",
            "gh pr view 5",
        ],
    )
    def test_a_mentioned_call_is_not_an_executed_one(self, command: str) -> None:
        assert forge_api_calls(command) == []

    def test_the_last_method_wins_and_a_bare_body_implies_a_write(self) -> None:
        assert ApiCall("x", "GET", has_body=True).is_write is False
        assert ApiCall("x", None, has_body=True).is_write is True
        assert ApiCall("x", None, has_body=False).is_write is False


class TestTraversalPrimitives:
    def test_a_heredoc_body_is_removed_but_its_head_and_delimiter_survive(self) -> None:
        stripped = strip_heredoc_bodies("cat <<EOF\ngh repo delete o/r\nEOF")

        assert "repo delete" not in stripped
        assert "<<EOF" in stripped

    def test_env_wrapper_and_compound_prefixes_are_consumed_before_the_program(self) -> None:
        assert program_words(["if", "TOKEN=x", "env", "FOO=1", "/usr/bin/gh", "repo"]) == ["/usr/bin/gh", "repo"]

    def test_env_split_string_is_walked_as_the_command_it_runs(self) -> None:
        assert program_words(["env", "-S", "gh repo delete", "o/r"]) == ["gh", "repo", "delete", "o/r"]

    def test_a_quoted_paren_does_not_end_a_substitution(self) -> None:
        assert command_substitution_bodies("echo \"$(printf ')'; gh repo delete o/r)\"") == [
            "printf ')'; gh repo delete o/r"
        ]

    def test_an_escaped_substitution_is_literal(self) -> None:
        assert command_substitution_bodies('echo "\\$(gh repo delete o/r)"') == []

    def test_a_wrapper_s_own_options_and_their_values_are_consumed(self) -> None:
        assert program_words(["env", "-u", "HOME", "-i", "time", "-p", "gh", "repo"]) == ["gh", "repo"]

    def test_a_segment_carrying_only_a_prefix_yields_no_program_word(self) -> None:
        assert program_words(["TOKEN=x", "(", ")"]) == []

    def test_each_separated_segment_is_lexed_on_its_own(self) -> None:
        assert segment_word_lists("echo hi && gh repo delete o/r") == [
            ["echo", "hi"],
            ["gh", "repo", "delete", "o/r"],
        ]
