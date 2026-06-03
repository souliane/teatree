r"""Body-file flag resolution for the pre-publish gates.

Split out of :mod:`teatree.hooks._command_parser` to keep that module under
the module-health LOC cap. This module owns one concern: resolving the body
a ``-F``/``--file``/``--body-file`` flag points at, including the cold-hook
fallback where the harness cwd has reset away from the worktree.

A ``git commit -F <relpath>`` body file is read with the cold PreToolUse hook
subprocess's cwd, which has often reset away from the worktree, so a relative
``-F`` path (or an absolute one the cold cwd cannot reach) is unreadable as
given. :func:`commit_body_file_base` resolves the dir whose repo the commit
LANDS in (the command's own ``cd``/``-C``/``--git-dir``), and
:func:`append_file_payload` retries the path against it — so the gate scans
the real body and the private-repo carve-out can downgrade it instead of
fail-closing on an unread body. A body file that exists nowhere still fails
closed, and a PUBLIC-repo body file still hard-blocks.

The matching primitives (``FAIL_CLOSED_SENTINEL``, ``_read_file_arg``,
``_attached_value``) stay in :mod:`_command_parser`; this module imports them
one-directionally. ``_command_parser`` calls back into here via a lazy import
(at call time, not module load) so no cycle forms.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from teatree.hooks._command_parser import FAIL_CLOSED_SENTINEL, attached_value, read_file_arg

# Long options that point at a FILE whose content we should read. If the
# file is missing or unreadable the parser appends the fail-closed sentinel.
_BODY_FILE_FLAG_NAMES: Final[frozenset[str]] = frozenset({"--body-file", "--description-file", "--file"})


@dataclass(frozen=True)
class BodyFileContext:
    """Resolution context for ``-F``/``--file``/``--body-file`` body files.

    Groups the three settings that flow together through the body-file
    walkers: the in-command ``heredoc_files`` map, the ``base`` dir a relative
    body file is retried against (the commit's repo dir), and
    ``fail_closed_body_file`` (what an UNREADABLE ``gh``/``glab`` body file
    does — the git ``-F`` commit-message path always fails closed regardless).
    """

    heredoc_files: dict[str, str]
    fail_closed_body_file: bool
    base: Path | None = None


def commit_body_file_base(command: str) -> Path | None:
    """Return the dir to resolve a ``git commit -F <relpath>`` body against.

    The base is the dir whose repo the commit LANDS in — the command's own
    leading ``cd``/``pushd`` plus ``-C``/``--git-dir`` directives, resolved
    by :func:`_commit_repo_dir.effective_repo_dir` and walked up to the
    enclosing repo root by :func:`_commit_repo_dir.git_root_for_dir`. ``None``
    when the command names no commit dir (a plain ``git commit`` whose body
    file is then resolved against the process cwd only) or when the dir is the
    fail-closed sentinel (a ``-C`` value the gate cannot pin down statically).
    """
    from teatree.hooks import _commit_repo_dir  # noqa: PLC0415

    repo_dir = _commit_repo_dir.effective_repo_dir(command)
    if repo_dir is None or repo_dir == _commit_repo_dir.UNRESOLVABLE_REPO_DIR:
        return None
    return _commit_repo_dir.git_root_for_dir(Path(repo_dir))


def walk_body_file_flags(words: list[str], payloads: list[str], *, is_git: bool, ctx: BodyFileContext) -> None:
    """Extract ``--body-file``/``--file``/``-F`` style file payloads.

    The git-style ``-F <path>`` form is a file reference ONLY for the
    ``git`` command (codex round-3 #6 — ``gh api -F body=x`` is a field
    assignment, NOT a file reference). The ``is_git`` flag scopes the
    short-form ``-F`` reader. The resolution context (heredoc map, repo-dir
    base, fail-closed policy) is carried by :class:`BodyFileContext`.
    """
    fail_closed = ctx.fail_closed_body_file
    i = 0
    n = len(words)
    while i < n:
        word = words[i]
        if word in _BODY_FILE_FLAG_NAMES and i + 1 < n:
            _append_file_payload(words[i + 1], payloads, ctx, fail_closed=fail_closed)
            i += 2
            continue
        attached: str | None = None
        for flag in _BODY_FILE_FLAG_NAMES:
            attached = attached_value(word, flag + "=")
            if attached is not None:
                _append_file_payload(attached, payloads, ctx, fail_closed=fail_closed)
                break
        if attached is not None:
            i += 1
            continue
        if is_git and word == "-F" and i + 1 < n:
            _append_file_payload(words[i + 1], payloads, ctx, fail_closed=True)
            i += 2
            continue
        if is_git:
            attached = attached_value(word, "-F")
            if attached is not None:
                _append_file_payload(attached, payloads, ctx, fail_closed=True)
                i += 1
                continue
        i += 1


def _append_file_payload(path: str, payloads: list[str], ctx: BodyFileContext, *, fail_closed: bool) -> None:
    """Append the body referenced by a ``-F``/``--file``/``--body-file`` path.

    Resolution order: the on-disk file (as-is, then relative to ``ctx.base`` --
    the commit's repo dir), then an in-command heredoc that writes to that
    path (``cat > path <<EOF … EOF``), then the ``fail_closed`` branch. The
    heredoc fallback closes the #126 false positive where a body written to a
    temp file and committed via ``-F`` in the same command was unreadable at
    PreToolUse scan time (the hook runs BEFORE the file is created). The
    ``ctx.base`` fallback closes the cold-hook false positive where the harness
    cwd has reset away from the worktree, so a ``git -C <worktree> commit -F
    <relpath>`` body file is unreadable from the cwd yet readable from the
    commit's own repo dir.

    ``fail_closed`` selects what an unresolvable path does. ``True`` appends
    the fail-closed sentinel: the ``git commit -F <path>`` commit-message path
    always uses it (#1207), as does a ``gh``/``glab`` body file for the
    destination-aware banned-terms / bare-reference scanners, so a PUBLIC post
    whose body the gate cannot read hard-blocks rather than slip through unread
    (a destination-internal post is skipped before the payload is scanned, so
    the sentinel never over-blocks it). ``False`` appends NOTHING — the quote
    scanner keeps a drafted-but-absent ``gh``/``glab`` body file as
    "needs-inline", not a fail-closed HIGH (#126).
    """
    content = read_file_arg(path, ctx.base)
    if content is None:
        content = ctx.heredoc_files.get(path)
    if content is not None:
        payloads.append(content)
    elif fail_closed:
        payloads.append(FAIL_CLOSED_SENTINEL)
