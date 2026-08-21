"""Recognise a hand-rolled "has this branch landed?" git probe (#4070).

A squash-merge rewrites the branch's commits into a NEW sha on the default
branch, so every per-commit / ancestor test — ``git cherry``, ``git branch
--merged``, ``git merge-base --is-ancestor``, ``git log … --not <default>`` —
reports the branch as un-landed while its whole content is already there. The
canonical answer is :mod:`teatree.core.worktree.branch_classification`'s three
content layers; this leaf spots an agent typing the primitive by hand so the
PreToolUse gate can point at the read-only front door instead.

What separates a landed-ness question from a legitimate use of the same
primitive is the TARGET: comparing against the repo's default branch is asking
"is this on main?", comparing against anything else is asking something else.
``core/management/commands/repro.py`` proves a RED sha is an ancestor of a GREEN
sha with the very same ``merge-base --is-ancestor``, and a coder rebasing runs
``git cherry`` against their own upstream — neither names a default branch, so
neither is recognised here.

The bare ``git branch --merged`` (no operand) IS recognised: with no commit
argument git answers against HEAD, which in a sweep across worktrees is the same
landed-ness question by another spelling.

Pure command analysis, no subprocess and no ORM, so the gate driving it stays
inside the fast-hook budget and the shapes are unit-testable.
"""

import re
from typing import Final

from teatree.hooks._shell_lexer import TokenKind, split_commands, tokenize

#: The shapes a default-branch target may be written as. Comparing against one of
#: these is what makes a probe a landed-ness question.
_DEFAULT_BRANCH_RE: Final[re.Pattern[str]] = re.compile(r"^(?:origin/)?(?:main|master)$")

_ENV_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
#: Leaders that run the NEXT word rather than being the command themselves.
_WRAPPER_LEADERS: Final[frozenset[str]] = frozenset({"command", "exec", "env", "nohup", "time", "stdbuf", "nice"})
#: git's leading global options that consume the following token as their value.
_GIT_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
)


def merged_detection_shape(command: str) -> str | None:
    """The hand-rolled landed-ness probe *command* runs, or ``None``.

    The return is a short human label of the matched primitive so the advisory can
    name what it saw. Every segment of the command is examined, so a probe behind a
    ``cd <worktree> &&`` is still recognised.
    """
    for words in _git_segments(command):
        if shape := _probe_shape(words):
            return shape
    return None


def _git_segments(command: str) -> list[list[str]]:
    """Every top-level segment whose command is ``git``, as its argument words.

    Quote-accurate: a probe quoted as ``echo 'git cherry origin/main HEAD'`` or
    passed as ``grep`` 's pattern is text, not an invocation, so it never surfaces
    here.
    """
    segments: list[list[str]] = []
    for segment in split_commands(tokenize(command)):
        words = [token.value for token in segment if token.kind is TokenKind.WORD]
        while words and (_ENV_ASSIGNMENT_RE.match(words[0]) or words[0] in _WRAPPER_LEADERS):
            words = words[1:]
        if words and words[0].rsplit("/", 1)[-1] == "git":
            segments.append(_past_git_globals(words[1:]))
    return segments


def _past_git_globals(args: list[str]) -> list[str]:
    """*args* from the subcommand on, skipping git's own leading global options."""
    cursor = 0
    while cursor < len(args) and args[cursor].startswith("-"):
        cursor += 2 if args[cursor] in _GIT_VALUE_FLAGS else 1
    return args[cursor:]


def _probe_shape(args: list[str]) -> str | None:
    """The landed-ness probe in one git invocation's ``[subcommand, *rest]``, or ``None``."""
    if not args:
        return None
    subcommand, rest = args[0], args[1:]
    if subcommand == "cherry" and _is_default_target(next(iter(_operands(rest)), None)):
        return "git cherry against the default branch"
    if subcommand == "branch" and _merged_flag_targets_default(rest):
        return "git branch --merged"
    if subcommand == "merge-base" and "--is-ancestor" in rest and any(map(_is_default_target, _operands(rest))):
        return "git merge-base --is-ancestor against the default branch"
    if subcommand == "log" and _is_default_target(_value_after(rest, "--not")):
        return "git log --not <default branch>"
    return None


def _merged_flag_targets_default(args: list[str]) -> bool:
    """Whether a ``git branch`` invocation asks the merged question about the default branch.

    ``--merged`` with no commit operand answers against HEAD; that is the same
    question, so it counts. An explicit NON-default operand does not.
    """
    for index, arg in enumerate(args):
        if attached := _attached_value(arg, "--merged"):
            return _is_default_target(attached)
        if arg == "--merged":
            operand = _first_operand(args[index + 1 :])
            return operand is None or _is_default_target(operand)
    return False


def _attached_value(token: str, flag: str) -> str | None:
    """The value of a ``--flag=value`` token, else ``None``."""
    if token.startswith(f"{flag}="):
        return token[len(flag) + 1 :]
    return None


def _value_after(args: list[str], flag: str) -> str | None:
    """The operand following *flag* (or its ``--flag=value`` form), else ``None``."""
    for index, arg in enumerate(args):
        if attached := _attached_value(arg, flag):
            return attached
        if arg == flag:
            return _first_operand(args[index + 1 :])
    return None


def _first_operand(args: list[str]) -> str | None:
    return next(iter(_operands(args)), None)


def _operands(args: list[str]) -> list[str]:
    """The non-flag words of *args* — the refs a probe compares."""
    return [arg for arg in args if not arg.startswith("-")]


def _is_default_target(ref: str | None) -> bool:
    """Whether *ref* names the repo's default branch — the landed-ness question's tell."""
    return ref is not None and bool(_DEFAULT_BRANCH_RE.match(ref))
