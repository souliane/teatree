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
    walkers: the in-command ``heredoc_files`` map (a body written earlier in
    the SAME command — a ``> path <<EOF`` heredoc or a ``printf``/``echo >
    path`` redirect — keyed by the redirect-target token), the ``base`` dir a
    relative body file is retried against (the commit's repo dir), and
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


@dataclass(frozen=True)
class _ShortFileFlag:
    """Resolved value of a short ``-F`` body-file reference, with its token span.

    ``path`` is the file the ``-F`` points at; ``consumed`` is how many tokens
    the flag occupied (``2`` for the space-separated ``-F <path>`` form, ``1``
    for the attached ``-F<path>`` form). ``fail_closed`` is the policy for an
    unreadable path — ``git``'s commit-message ``-F`` always fails closed, while
    ``gh``/``glab``'s body-file ``-F`` follows the destination-aware policy.
    ``None`` is returned when the ``-F`` at this position is not a body-file
    reference (so the caller advances by one and lets another walker handle it).
    """

    path: str
    consumed: int
    fail_closed: bool


def _short_f_body_file(leader: str, words: list[str], i: int, *, fail_closed: bool) -> _ShortFileFlag | None:
    """Resolve the file a short ``-F`` at ``words[i]`` references, or ``None``.

    The short ``-F`` is overloaded across leaders:

    - ``git`` -- ALWAYS a file (the ``git commit -F`` message file), regardless
        of the value, and always fails closed when unreadable (#1207).
    - ``gh`` / ``glab`` -- the documented short form of ``--body-file`` on
        ``issue/pr create|comment`` etc., but ``-F name=value`` on ``api`` is a
        field assignment. The two are disambiguated by VALUE: a ``=``-free token
        is a body-file path; a ``name=value`` token is left to
        :func:`_command_parser._walk_api_fields` (returns ``None`` here).
    - any other leader -- never a body-file ``-F`` (returns ``None``).

    Both the space-separated (``-F <path>``) and attached (``-F<path>``) spellings
    are recognised.
    """
    word = words[i]
    is_git = leader == "git"
    is_gh_glab = leader in {"gh", "glab"}
    if not (is_git or is_gh_glab):
        return None
    flag_fail_closed = True if is_git else fail_closed
    if word == "-F" and i + 1 < len(words):
        nxt = words[i + 1]
        if is_git or "=" not in nxt:
            return _ShortFileFlag(path=nxt, consumed=2, fail_closed=flag_fail_closed)
        return None
    attached = attached_value(word, "-F")
    if attached is not None and (is_git or "=" not in attached):
        return _ShortFileFlag(path=attached, consumed=1, fail_closed=flag_fail_closed)
    return None


def walk_body_file_flags(words: list[str], payloads: list[str], *, leader: str, ctx: BodyFileContext) -> None:
    """Extract ``--body-file``/``--file``/``-F`` style file payloads.

    The long ``--body-file`` / ``--description-file`` / ``--file`` forms apply to
    every leader. The short ``-F`` form is leader-scoped by
    :func:`_short_f_body_file`: ``git``'s ``-F`` is always the commit-message
    file; ``gh``/``glab``'s ``-F`` is the documented ``--body-file`` short form
    when its value is ``=``-free, otherwise it is an ``api`` field assignment
    that :func:`_command_parser._walk_api_fields` handles (#1812). The
    resolution context (heredoc map, repo-dir base, fail-closed policy) is
    carried by :class:`BodyFileContext`.
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
        short = _short_f_body_file(leader, words, i, fail_closed=fail_closed)
        if short is not None:
            _append_file_payload(short.path, payloads, ctx, fail_closed=short.fail_closed)
            i += short.consumed
            continue
        i += 1


def _append_file_payload(path: str, payloads: list[str], ctx: BodyFileContext, *, fail_closed: bool) -> None:
    """Append the body referenced by a ``-F``/``--file``/``--body-file`` path.

    Resolution order: the on-disk file (as-is, then relative to ``ctx.base`` --
    the commit's repo dir), then an in-command body written to that path — a
    ``cat > path <<EOF … EOF`` heredoc or a ``printf``/``echo > path`` redirect
    — then the ``fail_closed`` branch. The in-command fallback closes the #126
    false positive where a body written to a temp file and posted via
    ``--body-file``/``-F`` in the same command was unreadable at PreToolUse
    scan time (the hook runs BEFORE the file is created); the lookup key is the
    raw ``--body-file`` argument token, so a ``$f`` / ``$(mktemp)`` path matches
    the textually-identical redirect target even though neither is expanded.
    The ``ctx.base`` fallback closes the cold-hook false positive where the
    harness cwd has reset away from the worktree, so a ``git -C <worktree>
    commit -F <relpath>`` body file is unreadable from the cwd yet readable from
    the commit's own repo dir.

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
