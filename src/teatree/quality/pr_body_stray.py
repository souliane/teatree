"""Detect a stray PR/MR body file staged inside a worktree.

The ship flow's body scratch file belongs in the system temp dir
(:func:`teatree.utils.pr_body.pr_body_tempfile`), never in the repo. A hand-named
``pr-body.md`` / ``pr_body.md`` copied into the worktree and staged is
committable junk — the ``cp /tmp/pr-body.md pr-body.md`` pattern that leaks the
scratch body into git history. :func:`stray_pr_body_paths` names every staged
path whose basename looks like such a file so the ``check_pr_body_stray`` gate
can refuse the commit. Pure functions over the staged path list — no git, no
filesystem, no Django.
"""

import re
from collections.abc import Iterable

__all__ = ["block_message", "is_stray_pr_body", "stray_pr_body_paths"]

#: Anchored at the start so ``pr-body.md`` / ``pr_body-3537.md`` match while a
#: file merely containing the phrase (``my_pr_body_helper.py``) does not.
_STRAY_NAME_RE = re.compile(r"^pr[-_]body", re.IGNORECASE)


def is_stray_pr_body(path: str) -> bool:
    """Whether *path*'s basename looks like a hand-named PR-body scratch file.

    A PR body is a scratch text file, never Python source — the ``pr_body.py``
    helper module (and its ``test_pr_body.py`` mirror) must never be flagged, so
    a ``.py`` basename is excluded even though it matches the name prefix.
    """
    basename = path.replace("\\", "/").rsplit("/", 1)[-1]
    if basename.lower().endswith(".py"):
        return False
    return bool(_STRAY_NAME_RE.match(basename))


def stray_pr_body_paths(paths: Iterable[str]) -> list[str]:
    """Return the subset of *paths* that are stray PR-body files, order-preserving."""
    return [path for path in paths if is_stray_pr_body(path)]


def block_message(stray: list[str]) -> str:
    """The refusal shown when a PR-body scratch file is staged in the worktree.

    Names every offending path plus the fix: a PR body belongs in a system temp
    file the ship CLI owns (:func:`teatree.utils.pr_body.pr_body_tempfile`),
    never committed — remove it and let ``t3 <overlay> pr create`` build the body.
    """
    files = "\n".join(f"  - {path}" for path in stray)
    return (
        "Refusing commit: a PR/MR body scratch file is staged inside the worktree:\n\n"
        f"{files}\n\n"
        "A PR body belongs in a system temp file — the ship CLI owns it via "
        "`pr_body_tempfile`, never staged in the repo. Un-stage and delete it "
        "(`git rm --cached <path>` then remove the file), and let "
        "`t3 <overlay> pr create` build the body."
    )
