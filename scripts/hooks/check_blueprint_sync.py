"""Commit-msg hook: warn when src/ changes but the BLUEPRINT does not.

Exits non-zero when source code changes without a corresponding BLUEPRINT
update, unless the commit type is one that typically doesn't require it
(test, docs, style, chore, ci, fix, refactor, revert).

The "BLUEPRINT" is the top-level ``BLUEPRINT.md`` plus its split appendix
files under ``docs/blueprint/`` (e.g. ``configuration.md``,
``loop-topology.md``) — the appendices ARE the BLUEPRINT, so updating one of
them satisfies the sync requirement just as the monolith does.

The ``fix:``/``refactor:`` exemption only fires if the hook can read the commit
*type*, which lives in the commit message. The commit message is sourced
robustly: from ``argv[1]`` when prek hands it the commit-message file at the
``commit-msg`` stage, otherwise from git's canonical ``COMMIT_EDITMSG``. A
positional argument that is a staged *source* path (handed at the pre-commit
stage or by ``prek run --all-files``) is never mistaken for the commit message —
that coupling was the bug behind task #35, where a ``fix(db)`` commit was gated
because the first line of a staged source file was read as the "commit type".

A commit mid-``git merge`` is exempt regardless of message or staged files
(mirroring ``check_module_health.py``'s own merge exemption): its staged tree
carries every upstream commit's changes in one shot, so it would otherwise
false-block on virtually any non-BLUEPRINT upstream source commit.

A commit mid-``git revert`` is exempt regardless of message (its default
``Revert "..."`` message matches no prefix): the staged tree is the inverse
of a single original commit's diff, and if that original commit never
touched BLUEPRINT.md, undoing it can't need a BLUEPRINT update either. A
commit explicitly typed ``revert:``/``revert(scope):`` (Conventional
Commits' own revert type) is exempt by prefix the same way ``fix:`` is —
this covers a revert commit authored or replayed outside a live
``git revert`` operation (e.g. after a rebase), where ``REVERT_HEAD`` is
absent but the same reasoning still applies.

See: souliane/teatree#8
"""

import pathlib
import subprocess
import sys

# Commit types that don't require BLUEPRINT updates. ``refactor`` joins the
# set because a behaviour-preserving internal change (a swapped runner, an
# extracted helper) does not alter the external contracts BLUEPRINT documents
# — the same reasoning that exempts ``fix``. ``revert`` joins for the same
# reason as the ``_is_revert_commit`` REVERT_HEAD check: undoing a commit
# that never touched BLUEPRINT.md can't need a BLUEPRINT update either.
_EXEMPT_PREFIXES = ("test", "docs", "style", "chore", "ci", "fix", "refactor", "revert")

# Filenames git uses to hold an in-progress commit message. ``argv[1]`` is read
# as the commit message only when it ends in one of these — a staged ``src/``
# path never matches, so it can never be mis-read as the commit type.
_COMMIT_MSG_FILENAMES = ("COMMIT_EDITMSG", "MERGE_MSG", "SQUASH_MSG")


def _staged_files() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip().splitlines()


def _is_merge_commit() -> bool:
    """True mid-``git merge`` (``MERGE_HEAD`` exists), mirroring ``check_module_health.py``.

    A merge commit's staged tree carries every upstream commit's changes at
    once, including a ``src/`` change from a commit that never touched
    BLUEPRINT.md on its own — a routine ``git merge origin/main`` would
    otherwise false-block on virtually any non-BLUEPRINT upstream source
    commit, since the commit-type exemption can't apply (a merge's default
    message matches no ``fix:``/``refactor:``/etc. prefix).
    """
    result = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _is_revert_commit() -> bool:
    """True mid-``git revert`` (``REVERT_HEAD`` exists), mirroring ``_is_merge_commit``."""
    result = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "REVERT_HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _is_blueprint(path: str) -> bool:
    """A staged path that counts as a BLUEPRINT update.

    The top-level ``BLUEPRINT.md`` or any of its split appendix markdown files
    under ``docs/blueprint/`` — the appendices are the BLUEPRINT's deep-mechanics
    sections, so editing one satisfies the sync requirement.
    """
    return path == "BLUEPRINT.md" or (path.startswith("docs/blueprint/") and path.endswith(".md"))


def _looks_like_commit_msg_file(path: str) -> bool:
    """True when ``path`` is a git commit-message file, not a staged source path.

    Git (and prek at the ``commit-msg`` stage) hands the hook a path ending in
    one of git's known commit-message filenames. A staged ``src/`` path handed
    at another stage ends in ``.py`` / ``.md`` / etc. and never matches, so it
    is never read as the commit message.
    """
    return pathlib.PurePath(path).name in _COMMIT_MSG_FILENAMES


def _git_commit_editmsg_path() -> str:
    """Git's canonical path to the in-progress commit message (worktree-aware)."""
    result = subprocess.run(
        ["git", "rev-parse", "--git-path", "COMMIT_EDITMSG"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def _read_first_line(path: str) -> str:
    try:
        with pathlib.Path(path).open(encoding="utf-8") as fh:
            return fh.readline().strip()
    except OSError:
        return ""


def _commit_message() -> str:
    """The first line of the commit message, sourced robustly.

    Prefer ``argv[1]`` when it is genuinely the commit-message file git handed
    the hook at the ``commit-msg`` stage; otherwise fall back to git's canonical
    ``COMMIT_EDITMSG``. This keeps the commit type available to the exemption
    regardless of how the hook is invoked, and never reads a staged source path
    handed as ``argv[1]`` as if it were the commit message (task #35).
    """
    min_args = 2
    if len(sys.argv) >= min_args and _looks_like_commit_msg_file(sys.argv[1]):
        return _read_first_line(sys.argv[1])

    editmsg = _git_commit_editmsg_path()
    if editmsg:
        return _read_first_line(editmsg)
    return ""


def main() -> int:
    if _is_merge_commit() or _is_revert_commit():
        return 0

    msg = _commit_message()

    # Skip for commit types that don't need blueprint changes.
    msg_lower = msg.lower()
    if any(msg_lower.startswith((f"{prefix}:", f"{prefix}(")) for prefix in _EXEMPT_PREFIXES):
        return 0

    files = _staged_files()
    has_src = any(f.startswith("src/") for f in files)
    has_blueprint = any(_is_blueprint(f) for f in files)

    if has_src and not has_blueprint:
        print()
        print("  BLUEPRINT.md reminder:")
        print()
        print("    Source code changed but BLUEPRINT.md was not updated.")
        print("    If this change adds/removes/renames endpoints, models,")
        print("    commands, or config, please update BLUEPRINT.md too.")
        print()
        print("    To skip: use a fix/refactor/test/docs/style/chore/ci commit type.")
        print()
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
