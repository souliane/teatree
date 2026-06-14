"""Action-aware merge-subcommand detection (#2387).

The out-of-band-merge gate must fire only when the command INVOKES
``gh pr merge`` / ``glab mr merge`` as the executed program — never when the
phrase merely appears inside a heredoc body, a quoted argument, an
``echo``/``printf`` string, or a ``#`` comment.
"""

import pytest

from teatree.hooks.raw_merge_detect import invokes_raw_merge_subcommand


@pytest.mark.parametrize(
    "command",
    [
        "gh pr merge 5 --squash",
        "glab mr merge !9",
        "gh  pr  merge 5",  # double-space variant still an invocation
        "gh\tpr\tmerge 5",  # tab-separated variant
        "echo hi && gh pr merge 5",  # invocation after a separator
        "gh pr merge 5 # trailing comment",  # invocation with a trailing comment
    ],
)
def test_real_invocation_is_detected(command: str) -> None:
    assert invokes_raw_merge_subcommand(command) is True


@pytest.mark.parametrize(
    "command",
    [
        # Heredoc that documents the merge command (the #2387 over-block).
        "cat >> note.md <<EOF\nrun gh pr merge 5 to land the PR\nEOF",
        # Adversarial: a heredoc body line that BEGINS with the phrase.
        "cat >> note.md <<EOF\ngh pr merge 5 is the raw merge command\nEOF",
        # Bare heredoc to stdin that documents it.
        "cat <<EOF\ngh pr merge 5\nEOF",
        # echo / printf strings (single + double quoted).
        'echo "run gh pr merge 5"',
        "echo 'gh pr merge 5'",
        'printf "%s" "gh pr merge 5"',
        # The phrase inside a # comment.
        "ls  # gh pr merge 5",
        # The phrase as another command's argument.
        'grep "gh pr merge" file.txt',
        # An unrelated forge read.
        "gh pr view 3",
        # The REST-API form is handled by the separate api-write arm, not here.
        "gh api repos/o/r/pulls/12/merge -X PUT",
        "",
    ],
)
def test_documentation_or_mention_is_not_detected(command: str) -> None:
    assert invokes_raw_merge_subcommand(command) is False
