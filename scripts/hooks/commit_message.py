"""Shared helpers for hooks that need to peek at the in-progress commit message.

At ``commit-msg`` stage prek passes the message file as ``sys.argv[1]``. At
``pre-commit`` stage the file isn't passed, but ``.git/COMMIT_EDITMSG`` is
populated when the commit was started with ``-m`` or after the editor saves —
the helper falls back to that location.

The ``relax:`` (or ``relax(scope):``) prefix lets users acknowledge a
deliberate quality-gate relaxation that the hook would otherwise block.
"""

import pathlib
import re
import sys

_RELAX_PREFIX_RE = re.compile(r"^relax(\(.+\))?:")


def _find_git_dir() -> pathlib.Path | None:
    """Resolve the real .git directory (handles worktrees, where .git is a file)."""
    dot_git = pathlib.Path(".git")
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        text = dot_git.read_text(encoding="utf-8").strip()
        if text.startswith("gitdir: "):
            return pathlib.Path(text.removeprefix("gitdir: "))
    return None


def commit_message_has_relax_prefix() -> bool:
    """Return True when the in-progress commit message starts with ``relax:``.

    Resolves the message file from ``sys.argv[1]`` first, then falls back to
    ``.git/COMMIT_EDITMSG``. Returns False when neither is available — at
    pre-commit stage with a fresh commit, the message hasn't been written yet
    so the hook should block as usual.
    """
    msg_file = sys.argv[1] if len(sys.argv) > 1 else ""
    if not msg_file:
        git_dir = _find_git_dir()
        if git_dir:
            candidate = git_dir / "COMMIT_EDITMSG"
            if candidate.is_file():
                msg_file = str(candidate)
    if not msg_file:
        return False
    try:
        msg = pathlib.Path(msg_file).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, PermissionError):
        return False
    return bool(_RELAX_PREFIX_RE.match(msg))
