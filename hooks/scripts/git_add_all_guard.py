"""PreToolUse: refuse the whole-tree stage ``git add -A`` / ``git add .`` (#4093).

``git add -A`` stages whatever the working tree happens to hold. Four recorded
occurrences: a throwaway scratch file swept into a commit (caught only by
``tests/test_repo_root_minimal.py``, i.e. AFTER the commit existed and only
because it landed at the repo root), and — in a worktree shared with another
agent — that agent's in-progress edits committed under the wrong authorship.
The rule "stage explicit paths, keep scratch outside the repo" is written down
and has failed four times, so the deliverable is a gate, not another
restatement. A PreToolUse deny catches the whole class before anything is
staged, which is also when the fix is cheapest: the agent names its paths.

Deliberately narrow. ``git add <explicit paths>`` always passes, and so do
``git add -p`` and ``git add -u`` (tracked files only — no untracked sweep).
The verb is detected on the quote/heredoc-stripped SKELETON
(``mr_cli_fields.strip_quoted_and_heredoc``, as every sibling gate does), so the
phrase inside a commit message, a PR body, or a doc string is not an invocation.

NEVER-LOCKOUT: a per-call ``[add-all-ok: <reason>]`` token for the genuine
"yes, the whole tree" case (a first commit on a scaffolded directory), the
``[teatree] git_add_all_gate_enabled = false`` kill-switch
(``t3 <overlay> gate add-all disable``), and the shared ``_fail_open_or_deny``
chain (self-rescue allowlist + master fail-open + circuit breaker).

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with
no guarantee ``teatree`` is importable, so the module top imports only stdlib
and bare siblings — never Django / ``teatree.core``.
"""

import re
import sys
from pathlib import PurePosixPath
from typing import Final

from hooks.scripts.mr_cli_fields import strip_quoted_and_heredoc

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# re-exports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("git_add_all_guard", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.git_add_all_guard", sys.modules[__name__])

_ADD_ALL_OK_RE: Final[re.Pattern[str]] = re.compile(r"\[add-all-ok:\s*(\S[^\]]*?)\s*\]")
_TOKEN_SCAN_LIMIT: Final[int] = 512
_SEGMENT_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"\|\||&&|[;|&\n]")
_ENV_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
# Prefixes that run the NEXT word rather than being the command themselves.
_WRAPPER_LEADERS: Final[frozenset[str]] = frozenset({"command", "env", "nohup", "time", "stdbuf", "nice"})

# git's leading global options that consume the NEXT token as their value, so
# the subcommand scanner skips two tokens for them (``git -C <path> add``).
_GIT_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
)
# Flags that stage the whole tree INCLUDING untracked files — the swept class.
_SWEEP_FLAGS: Final[frozenset[str]] = frozenset({"-A", "--all", "--no-ignore-removal"})
# Flags that never sweep an untracked file: ``-u`` is tracked-only, ``-p``/``-i``
# stage hunk by hunk under the author's eye. A ``.`` pathspec alongside one of
# these is a scope narrowing, not a whole-tree sweep.
_NO_SWEEP_FLAGS: Final[frozenset[str]] = frozenset(
    {"-u", "--update", "-p", "--patch", "-i", "--interactive", "-n", "--dry-run"}
)
_WHOLE_TREE_PATHSPECS: Final[frozenset[str]] = frozenset({".", "./", ":/", "*"})


def deny_reason() -> str:
    """The one-line, actionable deny: name the explicit form and where scratch belongs."""
    return (
        "BLOCKED: `git add -A` / `git add .` stages the whole working tree — it has swept a "
        "scratch file (and another agent's in-progress edits, in a shared worktree) into a commit "
        "four times. Stage what you mean: `git add <path> <path>`. A temp file belongs in the "
        "session scratch directory (`~/.claude/jobs/<id>/tmp/`), never beside the code you are "
        "editing. `git add -p` and `git add -u` are fine. Genuinely the whole tree (a first commit "
        "on a scaffolded directory)? Put `[add-all-ok: <reason>]` in the `command` string."
    )


def is_whole_tree_stage(command: str) -> bool:
    """True iff *command* invokes ``git add`` over the whole working tree.

    Scanned on the quote/heredoc-stripped skeleton, so the phrase inside a commit
    message, a PR body or a heredoc is text rather than an invocation.
    """
    return any(
        _segment_stages_whole_tree(segment) for segment in _SEGMENT_SPLIT_RE.split(strip_quoted_and_heredoc(command))
    )


def ok_token(command: str) -> str | None:
    """Return the reason from an ``[add-all-ok: <reason>]`` token, else None."""
    match = _ADD_ALL_OK_RE.search(command[:_TOKEN_SCAN_LIMIT])
    if match is None:
        return None
    return match.group(1).strip() or None


def handle_block_git_add_all(data: dict) -> bool:
    """Deny a whole-tree ``git add``; allow everything else.

    Returns ``True`` when a deny was emitted (the caller stops the handler
    chain). A narrow targeted-command gate — it refuses exactly one command
    shape, never arbitrary Bash — and its deny still routes through
    ``_fail_open_or_deny`` so the self-rescue allowlist and the master
    kill-switch apply.
    """
    from hooks.scripts.hook_router import (  # noqa: PLC0415 deferred back-import
        _fail_open_or_deny,
        _teatree_bool_setting,
    )

    if data.get("tool_name") != "Bash":
        return False
    tool_input = data.get("tool_input", {})
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not isinstance(command, str) or not command:
        return False
    if not _teatree_bool_setting("git_add_all_gate_enabled", default=True):
        return False
    if not is_whole_tree_stage(command):
        return False
    if reason := ok_token(command):
        sys.stderr.write(f"NOTE: whole-tree stage allowed via [add-all-ok: {reason}].\n")
        return False
    return _fail_open_or_deny(data, deny_reason())


def _segment_stages_whole_tree(segment: str) -> bool:
    args = _git_add_args(segment.split())
    if args is None:
        return False
    if any(arg in _SWEEP_FLAGS or _short_cluster_has(arg, "A") for arg in args):
        return True
    if any(arg in _NO_SWEEP_FLAGS or _short_cluster_has(arg, "upin") for arg in args):
        return False
    return any(arg in _WHOLE_TREE_PATHSPECS for arg in _pathspecs(args))


def _git_add_args(tokens: list[str]) -> list[str] | None:
    """The arguments of a ``git add`` invocation in *tokens*, else None.

    ``git`` must LEAD the segment (past any env assignment or ``command``-style
    wrapper), so ``echo git add -A`` prints a string rather than staging one.
    Skips git's leading global options so ``git -C <path> add`` is still an
    ``add``; a path-form executable (``/usr/bin/git``) matches a bare ``git``.
    """
    index = 0
    while index < len(tokens) and (_ENV_ASSIGNMENT_RE.match(tokens[index]) or tokens[index] in _WRAPPER_LEADERS):
        index += 1
    if index >= len(tokens) or PurePosixPath(tokens[index]).name != "git":
        return None
    cursor = index + 1
    while cursor < len(tokens) and tokens[cursor].startswith("-"):
        cursor += 2 if tokens[cursor] in _GIT_VALUE_FLAGS else 1
    if cursor >= len(tokens) or tokens[cursor] != "add":
        return None
    return tokens[cursor + 1 :]


def _short_cluster_has(arg: str, letters: str) -> bool:
    """True iff *arg* is a clustered short-flag group carrying one of *letters* (``-Av``)."""
    if not arg.startswith("-") or arg.startswith("--") or len(arg) < 3:  # noqa: PLR2004 — a cluster is at least ``-Xy``
        return False
    return any(letter in arg[1:] for letter in letters)


def _pathspecs(args: list[str]) -> list[str]:
    """The positional pathspecs, with everything after a ``--`` separator included."""
    if "--" in args:
        return args[args.index("--") + 1 :]
    return [arg for arg in args if not arg.startswith("-")]
