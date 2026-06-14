"""Action-aware detection of a raw forge-merge subcommand — the out-of-band-merge gate (#2387).

The PreToolUse gate (BLUEPRINT §17.1 invariant 8) blocks a raw ``gh pr merge`` /
``glab mr merge`` on a teatree-managed repo because it bypasses the FSM keystone
merge. The original matcher searched for the subcommand phrase as a SUBSTRING
anywhere in the Bash command text, so a command that merely *documents* the merge
command — a ``cat >> note.md <<EOF … gh pr merge … EOF`` heredoc, an
``echo "run gh pr merge"`` string, or a ``# gh pr merge`` comment — was wrongly
blocked (same content-not-action over-block class as #1415).

This detection is action-aware: it fires only when the merge subcommand is the
EXECUTED program at a command position (the first words of a command segment),
not when the phrase appears inside a heredoc body, a quoted argument, an
``echo``/``printf`` string, or a comment. Heredoc bodies are stripped before
lexing; the rest falls out of the shared shell lexer (which drops comments and
keeps a quoted string as a single token) plus a command-position anchor that
matches ``safe_kill_detect``'s shape.
"""

import re

from teatree.hooks._shell_lexer import split_commands, tokenize

# A heredoc body span: ``<<['"]?DELIM['"]?\n … \nDELIM``. Stripped before lexing
# so a body line that BEGINS with the merge phrase cannot land at a command
# position. Mirrors the shape used by ``_body_file_resolution._HEREDOC_RE``.
_HEREDOC_BODY_RE = re.compile(r"(<<-?\s*['\"]?\w+['\"]?\s*\n).*?(\n\s*\w+\b)", re.DOTALL)

# The two forge merge subcommands as ordered leading words of a command segment.
_MERGE_SUBCOMMANDS: tuple[tuple[str, ...], ...] = (("gh", "pr", "merge"), ("glab", "mr", "merge"))


def _strip_heredoc_bodies(command: str) -> str:
    """Remove heredoc body content, keeping the redirect head and the delimiter line."""
    return _HEREDOC_BODY_RE.sub(lambda m: m.group(1) + m.group(2), command)


def invokes_raw_merge_subcommand(command: str) -> bool:
    """Whether *command* INVOKES ``gh pr merge`` / ``glab mr merge`` as an executed program.

    True only when a command segment's leading words are one of the merge
    subcommands. A heredoc body, a quoted argument, an ``echo``/``printf``
    string, and a ``#`` comment that merely mention the phrase are NOT matches.
    """
    if not command:
        return False
    for segment in split_commands(tokenize(_strip_heredoc_bodies(command))):
        words = tuple(token.value for token in segment)
        if any(words[: len(sub)] == sub for sub in _MERGE_SUBCOMMANDS):
            return True
    return False


__all__ = ["invokes_raw_merge_subcommand"]
