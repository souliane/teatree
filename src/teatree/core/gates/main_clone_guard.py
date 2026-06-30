"""Decision core for the main-clone working-tree protection gate (#2836).

A teatree main clone (a primary ``.git``-*dir* checkout, as opposed to a
linked ``.git``-*file* worktree) exists for exactly one purpose: to branch
worktrees from. Every development edit happens in a worktree, never in the
clone. The incident this gate closes: a sub-agent ran ``git checkout
<feature-branch>`` INSIDE the main clone, leaving it on a detached HEAD eight
commits behind ``origin/main`` and dirty. Because ``t3`` runs the editable
install FROM that clone, it then executed stale code and the housekeeping
self-update could not fast-forward the dirty/detached tree — so teatree went
silently stale.

This module is the pure, overlay-agnostic decision core: given a Bash command
plus the clone's default/protected branches, it classifies whether the command
is a working-tree mutation this gate forbids in a main clone. The thin
PreToolUse hook (``hooks/scripts/main_clone_guard.py``) supplies the
environmental facts — which repo encloses the cwd/file, whether it is a managed
main clone, the resolved default branch — and the deny emission.

The blocked set is precise (so the allowed hygiene ops are never locked out):
``git checkout``/``git switch`` to a NON-default branch, ``git reset --hard``,
``git restore``, and ``git stash pop``/``apply``. EVERYTHING else allows —
``git fetch``, ``git pull --ff-only``, ``git checkout <default>``,
``git worktree add/remove/prune/list``, and all read-only git — so ``t3
update`` and worktree creation keep working.
"""

import shlex
from dataclasses import dataclass

# Compound-command separators. A blocked verb hiding after ``&&``/``;``/``|`` in
# an otherwise-benign chain (``cd x && git checkout feature``) is still a
# main-clone mutation, so each segment is classified independently.
_SEGMENT_SEPARATORS = ("&&", "||", ";", "|", "&", "\n")

# git's leading global options that consume the NEXT token as their value, so
# the subcommand scanner skips two tokens for them (``git -C <path> checkout``).
_GLOBAL_OPTS_WITH_VALUE = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
)

# checkout/switch flags that move HEAD off the current branch independently of
# any positional target — a new branch or an explicit detach. Their presence
# alone makes the call a main-clone mutation, regardless of the branch name.
_CHECKOUT_MOVE_FLAGS = frozenset({"-b", "-B", "--orphan", "--detach"})
_SWITCH_MOVE_FLAGS = frozenset({"-c", "-C", "--create", "--force-create", "--orphan", "--detach"})


@dataclass(frozen=True, slots=True)
class MainCloneFinding:
    """A main-clone working-tree mutation the gate refuses.

    ``surface`` is ``"edit"`` (an Edit/Write to a path under the clone) or
    ``"git"`` (a forbidden git command run in the clone's cwd). ``target`` is
    the file path or the rendered git invocation, named in the deny message.
    """

    surface: str
    target: str


def edit_finding(file_path: str) -> MainCloneFinding:
    """The finding for an Edit/Write landing on a path inside a managed main clone."""
    return MainCloneFinding(surface="edit", target=file_path)


def find_main_clone_git_mutation(
    command: str,
    *,
    default_branch: str | None,
    protected_branches: frozenset[str],
) -> MainCloneFinding | None:
    """Return a finding when *command* mutates a main clone's working tree, else None.

    Only the precise blocked set fires: ``checkout``/``switch`` to a
    non-default branch, ``reset --hard``, ``restore``, ``stash pop``/``apply``.
    ``git fetch``, ``git pull --ff-only``, ``git checkout <default>``,
    ``git worktree …``, and every read-only git command return None (allow).
    """
    safe_branches = set(protected_branches)
    if default_branch:
        safe_branches.add(default_branch)
    for segment in _segments(command):
        call = _git_call(segment)
        if call is None:
            continue
        subcommand, args = call
        if _is_blocked(subcommand, args, safe_branches):
            return MainCloneFinding(surface="git", target=_render(subcommand, args))
    return None


def deny_reason(finding: MainCloneFinding) -> str:
    """The FAIL-LOUD deny message pointing the agent at branching a worktree."""
    what = f"editing `{finding.target}` in" if finding.surface == "edit" else f"`{finding.target}` against"
    return (
        f"BLOCKED: {what} a teatree-managed MAIN CLONE's working tree. The main "
        "clone exists only to branch worktrees from — never edit it, switch it to "
        "a feature branch, or reset/restore/stash-pop it (a dirty/detached main "
        "clone makes the editable `t3` run stale code, #2836). Branch a worktree "
        "off origin/main instead:\n"
        "  t3 <overlay> workspace ticket <issue-url-or-id>\n"
        "or: git worktree add -b <branch> ../<repo>-wt origin/main\n"
        "Read-only git, `git fetch`, `git pull --ff-only`, `git checkout <default>`, "
        "and `git worktree add/remove/prune/list` are always allowed. Vetted "
        "one-off: append `[main-clone-ok: <reason>]` to the command."
    )


def _segments(command: str) -> list[str]:
    """Split a compound shell command into independently-classified segments."""
    segments = [command]
    for sep in _SEGMENT_SEPARATORS:
        segments = [piece for seg in segments for piece in seg.split(sep)]
    return [seg.strip() for seg in segments if seg.strip()]


def _git_call(segment: str) -> tuple[str, list[str]] | None:
    """Return ``(subcommand, args)`` for a ``git`` invocation in *segment*, else None.

    Skips git's leading global options (``-C <path>``, ``-c k=v``,
    ``--no-pager`` …) so the real subcommand is found. A segment with no
    parseable ``git`` token, or one whose quotes don't balance, yields None
    (fail open — never block on an unparsable command).
    """
    tokens = _safe_split(segment)
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
        cursor += 2 if base in _GLOBAL_OPTS_WITH_VALUE and "=" not in token else 1
    return None


def _safe_split(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return []


def _is_blocked(subcommand: str, args: list[str], safe_branches: set[str]) -> bool:
    if subcommand == "reset":
        return "--hard" in args
    if subcommand == "restore":
        return True
    if subcommand == "stash":
        return bool(args) and args[0] in {"pop", "apply"}
    if subcommand in {"checkout", "switch"}:
        return _checkout_switch_blocked(subcommand, args, safe_branches)
    return False


def _checkout_switch_blocked(subcommand: str, args: list[str], safe_branches: set[str]) -> bool:
    """True iff a checkout/switch leaves the main clone off its default branch.

    A create/detach flag (``-b``/``-c``/``--detach`` …) or a pathspec restore
    (``--`` operand) always blocks. Otherwise the first move target is the first
    positional: blocked unless it is the resolved default branch or a protected
    branch. A bare ``git checkout`` with no move target moves nothing, so it
    allows.

    A lone ``-`` (``git checkout -`` / ``git switch -``, toggle to the PREVIOUS
    branch) is a move target, not a flag — it lands the clone off the default
    just as ``@{-1}`` does — so it is treated as a positional and blocks. This
    is the exact incident class: ``git checkout -`` from a main-clone cwd
    silently switches the clone to whatever it was last on.
    """
    move_flags = _CHECKOUT_MOVE_FLAGS if subcommand == "checkout" else _SWITCH_MOVE_FLAGS
    if any(arg in move_flags for arg in args):
        return True
    if "--" in args:
        return True
    target = next((arg for arg in args if _is_move_target(arg)), None)
    if target is None:
        return False
    return target not in safe_branches


def _is_move_target(arg: str) -> bool:
    """Whether *arg* is a positional move target (a branch/ref), not a flag.

    A bare ``-`` is git's "previous branch" shorthand — a positional, never a
    flag — so it counts as a move target. Every other ``-``-prefixed token is a
    flag (handled by the move-flag / ``--`` checks); a non-dash token is a
    plain ref.
    """
    return arg == "-" or not arg.startswith("-")


def _render(subcommand: str, args: list[str]) -> str:
    return " ".join(["git", subcommand, *args]).strip()
