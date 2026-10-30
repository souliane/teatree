"""Pure parse of a shell command into the ``git`` calls the clone guards classify."""

import shlex

# A blocked verb hiding after ``&&``/``;``/``|`` in an otherwise-benign chain
# (``cd x && git checkout feature``) is still a violation, so each segment is classified independently.
SEGMENT_SEPARATORS = ("&&", "||", ";", "|", "&", "\n")

# git's leading global options that consume the NEXT token as their value, so the
# subcommand scanner skips two tokens for them (``git -C <path> checkout``).
GLOBAL_OPTS_WITH_VALUE = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
)


def command_segments(command: str) -> list[str]:
    """Split a compound shell command into independently-classified segments."""
    segments = [command]
    for sep in SEGMENT_SEPARATORS:
        segments = [piece for seg in segments for piece in seg.split(sep)]
    return [seg.strip() for seg in segments if seg.strip()]


def safe_split(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return []


def git_call(segment: str) -> tuple[str, list[str]] | None:
    """Return ``(subcommand, args)`` for a ``git`` invocation in *segment*, else None.

    Skips git's leading global options (``-C <path>``, ``-c k=v``,
    ``--no-pager`` …) so the real subcommand is found. A segment with no
    parseable ``git`` token, or one whose quotes don't balance, yields None
    (fail open — never block on an unparsable command).
    """
    tokens = safe_split(segment)
    try:
        index = tokens.index("git")
    except ValueError:
        return None
    cursor = index + 1
    while cursor < len(tokens):
        token = tokens[cursor]
        if not token.startswith("-"):
            return token, tokens[cursor + 1 :]
        base = token.split("=", 1)[0]
        cursor += 2 if base in GLOBAL_OPTS_WITH_VALUE and "=" not in token else 1
    return None
