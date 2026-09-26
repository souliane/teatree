"""Action-aware MR/PR-create-invocation detection.

Mirrors ``test_raw_merge_detect.py``: the out-of-band CREATE gate fires on ANY
plausible invocation of ``gh pr create`` / ``glab mr create`` as an executed
program (env-prefixed, wrapper-prefixed, path-qualified, grouped/compound, or
inside a command substitution) and allows through only provably-non-invocation
text — a heredoc body, a ``#`` comment, and a quoted-string operand.
"""

import pytest

from teatree.hooks.raw_create_detect import (
    invokes_raw_create_subcommand,
    is_raw_create_api_write,
    raw_create_deny_reason,
    raw_create_targets,
)

_STILL_BLOCKED = [
    pytest.param("gh pr create --title x --body y", id="bare-gh"),
    pytest.param("glab mr create --title x", id="bare-glab"),
    pytest.param("gh  pr  create --title x", id="double-space"),
    pytest.param("gh\tpr\tcreate --title x", id="tab-separated"),
    pytest.param("   gh pr create --title x", id="leading-whitespace"),
    pytest.param("gh pr \\\n  create --title x", id="line-continuation"),
    pytest.param("gh pr create", id="no-args"),
    pytest.param("gh pr create --title x # trailing comment", id="trailing-comment"),
    pytest.param("echo hi && gh pr create --title x", id="after-and"),
    pytest.param("echo hi || gh pr create --title x", id="after-or"),
    pytest.param("echo hi ; gh pr create --title x", id="after-semicolon"),
    pytest.param("echo hi | gh pr create --title x", id="after-pipe"),
    pytest.param("echo hi & gh pr create --title x", id="after-background"),
    pytest.param("GH_TOKEN=x gh pr create --title x", id="env-assignment-prefix"),
    pytest.param("command gh pr create --title x", id="wrapper-command"),
    pytest.param("time gh pr create --title x", id="wrapper-time"),
    pytest.param("nohup gh pr create --title x", id="wrapper-nohup"),
    pytest.param("exec gh pr create --title x", id="wrapper-exec"),
    pytest.param("xargs gh pr create", id="wrapper-xargs"),
    pytest.param("env gh pr create --title x", id="wrapper-env"),
    pytest.param("env FOO=bar gh pr create --title x", id="wrapper-env-with-assignment"),
    pytest.param("/usr/bin/gh pr create --title x", id="path-qualified-basename"),
    pytest.param("echo $(gh pr create --title x)", id="command-substitution-dollar"),
    pytest.param("echo $(echo $(gh pr create --title x))", id="command-substitution-nested"),
    pytest.param("x=$(gh pr create --title x)", id="command-substitution-assigned"),
    pytest.param("echo `gh pr create --title x`", id="command-substitution-backtick"),
    pytest.param("result=`gh pr create --title x`", id="command-substitution-backtick-assigned"),
    pytest.param('echo "$(gh pr create --title x)"', id="command-substitution-in-double-quotes"),
    pytest.param("cat <<EOF\n$(gh pr create --title x)\nEOF", id="command-substitution-in-heredoc-body"),
    pytest.param("( gh pr create --title x )", id="subshell-group"),
    pytest.param("{ gh pr create --title x; }", id="brace-group"),
    pytest.param("if true; then gh pr create --title x; fi", id="compound-if-then"),
    pytest.param("env -i gh pr create --title x", id="wrapper-env-with-option"),
    pytest.param("time -p glab mr create --title x", id="wrapper-time-with-option"),
    pytest.param("command -- gh pr create --title x", id="wrapper-command-end-of-options"),
    pytest.param('env -S "gh pr create --title x"', id="wrapper-env-split-string"),
    pytest.param("env -S'glab mr create --title x'", id="wrapper-env-split-string-glued"),
    pytest.param("echo \"$(printf ')'; gh pr create --title x)\"", id="quoted-paren-inside-substitution"),
]

_STILL_ALLOWED = [
    pytest.param("cat >> note.md <<EOF\nrun gh pr create to open the PR\nEOF", id="heredoc-documents"),
    pytest.param(
        "cat >> note.md <<EOF\ngh pr create is the raw create command\nEOF",
        id="heredoc-body-begins-with-phrase",
    ),
    pytest.param("cat <<EOF\ngh pr create --title x\nEOF", id="bare-heredoc-documents"),
    pytest.param('echo "run gh pr create --title x"', id="echo-double-quoted"),
    pytest.param("echo 'gh pr create --title x'", id="echo-single-quoted"),
    pytest.param('printf "%s" "gh pr create --title x"', id="printf-quoted-operand"),
    pytest.param("ls  # gh pr create --title x", id="comment"),
    pytest.param('grep "gh pr create" file.txt', id="quoted-argument"),
    pytest.param("gh pr view 3", id="unrelated-forge-read"),
    pytest.param("gh api repos/o/r/pulls -X POST", id="rest-api-form-handled-elsewhere"),
    pytest.param("GH_TOKEN=x VERBOSE=1", id="only-env-assignments-no-program"),
    pytest.param("( )", id="only-grouping-no-program"),
    pytest.param("command env", id="only-wrapper-no-program"),
    pytest.param("echo '$(gh pr create --title x)'", id="single-quoted-substitution-is-literal"),
    pytest.param('echo "\\$(gh pr create --title x)"', id="escaped-substitution-is-literal"),
    pytest.param("", id="empty"),
]


@pytest.mark.parametrize("command", _STILL_BLOCKED)
def test_glab_mr_create_at_command_position_is_a_create(command: str) -> None:
    assert invokes_raw_create_subcommand(command) is True


@pytest.mark.parametrize("command", _STILL_ALLOWED)
def test_heredoc_body_and_quoted_operand_are_not_creates(command: str) -> None:
    assert invokes_raw_create_subcommand(command) is False


class TestCreateApiWrite:
    @pytest.mark.parametrize(
        "command",
        [
            "gh api repos/o/r/pulls -f title=x -f head=a -f base=b",
            "gh api repos/o/r/pulls --method POST -f title=x",
            "gh api repos/o/r/pulls -XPOST -f title=x",
            "glab api projects/9/merge_requests -f title=x",
            "glab api projects/9/merge_requests -X POST -f title=x",
            "glab api projects/9/merge_requests/ -f title=x",  # collection, trailing slash
            "gh api repos/o/r/pulls -ftitle=x",  # glued field flag
            "glab api projects/9/merge_requests -Ftitle=x",
            "glab api --output ndjson projects/9/merge_requests -f title=x",  # value option before the endpoint
        ],
    )
    def test_post_to_merge_requests_collection_is_a_create_but_get_is_not(self, command: str) -> None:
        assert is_raw_create_api_write(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            "gh api repos/o/r/pulls",  # bare GET lists PRs
            "gh api repos/o/r/pulls -X GET",
            "gh api repos/o/r/pulls -X POST -X GET",  # last-wins GET
            "gh api repos/o/r/pulls -X=GET -f title=x",  # short option with "="
            "glab api projects/9/merge_requests",
            "echo 'gh api repos/o/r/pulls -f title=x'",
            "cat <<EOF\nglab api projects/9/merge_requests -f title=x\nEOF",
            "",
            "ls",
        ],
    )
    def test_reads_of_the_collection_endpoint_are_not_creates(self, command: str) -> None:
        assert is_raw_create_api_write(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "glab api projects/9/merge_requests/42 -X PUT -f title=x",
            "gh api repos/o/r/pulls/42 -f title=x",
            "glab api projects/9/merge_requests/42/notes -f body=x",
            "gh api repos/o/r/pulls/comments/123/replies -f body=x",
        ],
    )
    def test_post_to_merge_requests_iid_is_not_a_create(self, command: str) -> None:
        assert is_raw_create_api_write(command) is False


class TestRawCreateTargets:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("gh pr create --repo a/b --title x; glab mr create --title y", ["a/b", None]),
            ("echo '--repo a/b' && glab mr create --title x", [None]),
            ("gh pr create -Ra/b", ["a/b"]),
            ("gh pr create --repo=a/b", ["a/b"]),
            ("gh api repos/o/r/pulls -f title=x", ["o/r"]),
            ("glab api projects/acme%2Fwidget/merge_requests -f title=x", ["acme/widget"]),
            ("glab api projects/9/merge_requests -f title=x", ["9"]),
            ("gh pr view 5", []),
        ],
    )
    def test_each_executed_create_carries_only_its_own_target(self, command: str, expected: list[str | None]) -> None:
        assert raw_create_targets(command) == expected


class TestRawCreateDenyReason:
    @pytest.mark.parametrize(
        "command",
        [
            "gh pr create --title x --body y",
            "glab mr create --title x",
            "gh api repos/o/r/pulls -f title=x",
            "glab api projects/9/merge_requests -X POST -f title=x",
        ],
    )
    def test_every_create_vector_yields_a_reason(self, command: str) -> None:
        reason = raw_create_deny_reason(command)
        assert reason is not None
        assert "ensure-pr" in reason

    @pytest.mark.parametrize(
        "command",
        ["gh pr view 5", "ls -la", "", "gh api repos/o/r/pulls/42 -f title=x", "glab mr update 5 --title x"],
    )
    def test_non_create_commands_yield_no_reason(self, command: str) -> None:
        assert raw_create_deny_reason(command) is None
