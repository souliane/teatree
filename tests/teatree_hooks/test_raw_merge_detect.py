"""Action-aware merge-invocation detection (#2387).

The out-of-band-merge gate is a STRICT tightening of the old substring matcher:
it fires on ANY plausible invocation of ``gh pr merge`` / ``glab mr merge`` as an
executed program (env-prefixed, wrapper-prefixed, path-qualified,
grouped/compound, or inside a command substitution) and allows through only
provably-non-invocation text — a heredoc body, a ``#`` comment, and a
quoted-string operand.

The ``id=`` on each evasion case names the handling whose removal would turn the
case RED, so a regression that drops env/wrapper/path/substitution awareness
fails the specific id.
"""

import pytest

from teatree.hooks.raw_merge_detect import invokes_raw_merge_subcommand

_STILL_BLOCKED = [
    pytest.param("gh pr merge 5 --squash", id="bare-gh"),
    pytest.param("glab mr merge !9", id="bare-glab"),
    pytest.param("gh  pr  merge 5", id="double-space"),
    pytest.param("gh\tpr\tmerge 5", id="tab-separated"),
    pytest.param("   gh pr merge 5", id="leading-whitespace"),
    pytest.param("gh pr \\\n  merge 5", id="line-continuation"),
    pytest.param("gh pr merge", id="no-args"),
    pytest.param("gh pr merge 5 # trailing comment", id="trailing-comment"),
    pytest.param("echo hi && gh pr merge 5", id="after-and"),
    pytest.param("echo hi || gh pr merge 5", id="after-or"),
    pytest.param("echo hi ; gh pr merge 5", id="after-semicolon"),
    pytest.param("echo hi | gh pr merge 5", id="after-pipe"),
    pytest.param("echo hi & gh pr merge 5", id="after-background"),
    pytest.param("GH_TOKEN=x gh pr merge 5", id="env-assignment-prefix"),
    pytest.param("command gh pr merge 5", id="wrapper-command"),
    pytest.param("time gh pr merge 5", id="wrapper-time"),
    pytest.param("nohup gh pr merge 5", id="wrapper-nohup"),
    pytest.param("exec gh pr merge 5", id="wrapper-exec"),
    pytest.param("xargs gh pr merge", id="wrapper-xargs"),
    pytest.param("env gh pr merge 5", id="wrapper-env"),
    pytest.param("env FOO=bar gh pr merge 5", id="wrapper-env-with-assignment"),
    pytest.param("/usr/bin/gh pr merge 5", id="path-qualified-basename"),
    pytest.param("echo $(gh pr merge 5)", id="command-substitution-dollar"),
    pytest.param("echo $(echo $(gh pr merge 5))", id="command-substitution-nested"),
    pytest.param("x=$(gh pr merge 5)", id="command-substitution-assigned"),
    pytest.param("echo `gh pr merge 5`", id="command-substitution-backtick"),
    pytest.param("result=`gh pr merge 5`", id="command-substitution-backtick-assigned"),
    pytest.param('echo "$(gh pr merge 5)"', id="command-substitution-in-double-quotes"),
    pytest.param("cat <<EOF\n$(gh pr merge 5)\nEOF", id="command-substitution-in-heredoc-body"),
    pytest.param("( gh pr merge 5 )", id="subshell-group"),
    pytest.param("{ gh pr merge 5; }", id="brace-group"),
    pytest.param("if true; then gh pr merge 5; fi", id="compound-if-then"),
]

_STILL_ALLOWED = [
    pytest.param("cat >> note.md <<EOF\nrun gh pr merge 5 to land the PR\nEOF", id="heredoc-documents"),
    pytest.param(
        "cat >> note.md <<EOF\ngh pr merge 5 is the raw merge command\nEOF",
        id="heredoc-body-begins-with-phrase",
    ),
    pytest.param("cat <<EOF\ngh pr merge 5\nEOF", id="bare-heredoc-documents"),
    pytest.param('echo "run gh pr merge 5"', id="echo-double-quoted"),
    pytest.param("echo 'gh pr merge 5'", id="echo-single-quoted"),
    pytest.param('printf "%s" "gh pr merge 5"', id="printf-quoted-operand"),
    pytest.param("ls  # gh pr merge 5", id="comment"),
    pytest.param('grep "gh pr merge" file.txt', id="quoted-argument"),
    pytest.param("gh pr view 3", id="unrelated-forge-read"),
    pytest.param("gh api repos/o/r/pulls/12/merge -X PUT", id="rest-api-form-handled-elsewhere"),
    pytest.param("GH_TOKEN=x VERBOSE=1", id="only-env-assignments-no-program"),
    pytest.param("( )", id="only-grouping-no-program"),
    pytest.param("command env", id="only-wrapper-no-program"),
    pytest.param("", id="empty"),
]


@pytest.mark.parametrize("command", _STILL_BLOCKED)
def test_plausible_invocation_blocks(command: str) -> None:
    assert invokes_raw_merge_subcommand(command) is True


@pytest.mark.parametrize("command", _STILL_ALLOWED)
def test_documentation_or_mention_is_allowed(command: str) -> None:
    assert invokes_raw_merge_subcommand(command) is False
