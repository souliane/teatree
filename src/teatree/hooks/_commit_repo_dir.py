r"""Resolve the dir whose repo a ``git`` command's commit LANDS in.

Split out of :mod:`teatree.hooks.publish_surface` to keep that module under
the module-health LOC cap. This module owns one concern: from a Bash command
string, statically resolve the working directory ``git`` would use, mirroring
``git``'s documented global-flag semantics so the banned-terms carve-out
decides privacy from the repo the commit ACTUALLY lands in.

``git`` selects a commit's repo as: the ``--git-dir``/``$GIT_DIR`` repo if
specified, else the repo discovered from the effective working directory,
which a leading ``cd <dir>`` / ``pushd <dir>`` and ``-C <dir>`` change.
Repeated ``-C`` is CUMULATIVE (each non-absolute ``-C <path>`` is relative to
the preceding one; an absolute ``-C`` resets); repeated ``--git-dir`` is
last-wins. ``--work-tree`` only sets the working tree and NEVER selects the
repo, so it is excluded -- a ``--git-dir <PUBLIC> --work-tree <PRIVATE>``
commit lands in the PUBLIC repo.

A leading ``cd <dir> &&`` / ``pushd <dir> &&`` navigation prefix is parsed
the same way ``-C`` is: at PreToolUse the ambient hook cwd is often the
workspace root (not the worktree the agent ``cd``'d into), so honouring the
in-command ``cd`` is what pins a bare ``git commit`` to the repo it actually
lands in. ``git``'s own ``-C`` is applied ON TOP of that ``cd`` dir.

Fail closed: a ``-C`` value the gate cannot resolve statically (e.g. a
substitution marker) yields :data:`UNRESOLVABLE_REPO_DIR`, and the carve-out
must then refuse to downgrade rather than guess a target.

:func:`git_root_for_dir` walks UP from a resolved dir to the nearest
enclosing ``.git`` so a commit run from a SUBDIR of a worktree still resolves
to the worktree's repo (and so the carve-out can tell a genuinely-unresolvable
commit -- no enclosing repo anywhere -- from a resolvable one).
"""

from pathlib import Path
from typing import Final

from teatree.hooks._command_parser import first_segment_words
from teatree.hooks._gh_glab_hiding import command_segments, token_has_substitution_marker

# Returned by ``effective_repo_dir`` when a ``-C`` value cannot be resolved
# statically (e.g. it carries a substitution marker). The commit carve-out
# treats this as an UNKNOWN target and refuses to downgrade -- fail closed,
# never leak onto a target we cannot pin down.
UNRESOLVABLE_REPO_DIR: Final[str] = "\x00teatree-unresolvable-repo-dir\x00"

# A ``cd <path>`` / ``pushd <path>`` navigation segment needs the verb plus
# its single path argument.
_NAV_WITH_PATH_WORD_COUNT: Final[int] = 2
_NAV_VERBS: Final[frozenset[str]] = frozenset({"cd", "pushd"})


def _last_flag_value(words: list[str], flag: str) -> str | None:
    """Return the LAST ``flag <value>`` / ``flag=<value>`` value, or ``None``.

    ``git`` resolves a repeated ``--git-dir`` LAST-WINS, so this scans the
    whole word list and keeps the final occurrence across both the
    space-separated and ``=`` spellings.
    """
    found: str | None = None
    i = 0
    prefix = flag + "="
    while i < len(words):
        w = words[i]
        if w == flag and i + 1 < len(words):
            found = words[i + 1]
            i += 2
            continue
        if w.startswith(prefix):
            found = w[len(prefix) :]
        i += 1
    return found


def _cumulative_dash_c(words: list[str]) -> str | None:
    """Return the working dir a chain of ``-C`` flags resolves to, or ``None``.

    Mirrors ``git``'s documented cumulative ``-C`` semantics: each subsequent
    NON-absolute ``-C <path>`` is interpreted relative to the preceding
    ``-C <path>``, while an absolute ``-C <path>`` RESETS the accumulator.
    Returns ``None`` when no ``-C`` is present, or the fail-closed sentinel
    :data:`UNRESOLVABLE_REPO_DIR` when a ``-C`` value carries a substitution
    marker (a value the gate cannot resolve statically -- the caller must then
    NOT downgrade).
    """
    accumulator: Path | None = None
    i = 0
    while i < len(words):
        value: str | None = None
        if words[i] == "-C" and i + 1 < len(words):
            value = words[i + 1]
            i += 2
        elif words[i].startswith("-C="):
            value = words[i][len("-C=") :]
            i += 1
        else:
            i += 1
            continue
        if token_has_substitution_marker(value):
            return UNRESOLVABLE_REPO_DIR
        path = Path(value)
        accumulator = path if path.is_absolute() or accumulator is None else accumulator / value
    return str(accumulator) if accumulator is not None else None


def leading_cd_dir(command: str) -> str | None:
    """Return the working dir a leading ``cd``/``pushd`` chain resolves to, or ``None``.

    Walks the LEADING navigation segments of ``command`` (``cd <path>`` /
    ``pushd <path>`` separated by ``&&``/``;``/...), stopping at the first
    non-navigation segment (the ``git commit``). Each subsequent non-absolute
    path joins onto the preceding one; an absolute path resets the accumulator,
    mirroring shell semantics. ``None`` when no leading ``cd``/``pushd`` is
    present, so the caller falls back to the ambient cwd.
    """
    accumulator: Path | None = None
    for words in command_segments(command):
        if len(words) < _NAV_WITH_PATH_WORD_COUNT or words[0] not in _NAV_VERBS:
            break
        value = words[1]
        path = Path(value)
        accumulator = path if path.is_absolute() or accumulator is None else accumulator / value
    return str(accumulator) if accumulator is not None else None


def git_root_for_dir(start: Path) -> Path | None:
    """Return the nearest enclosing ``.git`` worktree/repo root of ``start``, or ``None``.

    Walks UP from ``start`` (inclusive) until a directory containing a ``.git``
    entry (a dir for a normal clone, a file for a worktree/submodule) is found.
    ``None`` when no enclosing repo exists -- the commit dir is not inside any
    git repo, which the carve-out reads as a genuinely-unresolvable LOCAL
    commit (git itself would reject it; a non-repo commit cannot leak).
    """
    try:
        current = start.resolve()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def effective_repo_dir(command: str) -> str | None:
    """Return the dir whose repo a ``git`` command's commit LANDS in, or ``None``.

    A leading ``cd <dir>`` / ``pushd <dir>`` navigation prefix
    (:func:`leading_cd_dir`) sets the base working dir; ``git``'s own ``-C``
    is applied ON TOP of it. Repeated ``-C`` flags are CUMULATIVE, not
    last-wins: each subsequent non-absolute ``-C <path>`` joins onto the
    preceding one and an absolute ``-C <path>`` resets the accumulator
    (:func:`_cumulative_dash_c`), matching ``git``'s documented behaviour.
    Repeated ``--git-dir`` IS last-wins. ``--work-tree`` never selects the
    repo and is excluded.

    Resolution: ``--git-dir`` (last-wins) if present, resolved relative to the
    accumulated ``-C``/``cd`` dir when relative; else the accumulated
    ``-C``/``cd`` dir. ``None`` when no ``cd``/``-C``/``--git-dir`` is present,
    so the caller falls back to the ambient cwd for a plain ``git commit``. The
    fail-closed sentinel :data:`UNRESOLVABLE_REPO_DIR` is returned when a
    ``-C`` value cannot be statically resolved, so the carve-out never
    downgrades onto an unknown target.
    """
    cd_dir = leading_cd_dir(command)
    git_words = _git_segment_words(command)
    dash_c = _cumulative_dash_c(git_words)
    if dash_c == UNRESOLVABLE_REPO_DIR:
        return UNRESOLVABLE_REPO_DIR
    base = _combine_base(cd_dir, dash_c)
    git_dir = _last_flag_value(git_words, "--git-dir")
    if git_dir is not None:
        if base is not None and not Path(git_dir).is_absolute():
            return str(Path(base) / git_dir)
        return git_dir
    return base


def _git_segment_words(command: str) -> list[str]:
    """Return the word list of the ``git`` commit segment.

    The ``git`` invocation may sit behind a leading ``cd``/``pushd``
    navigation prefix, so the FIRST segment is not always the ``git`` one.
    Returns the first segment whose command word is ``git``, else the first
    segment (so a plain ``git commit`` with no ``cd`` prefix is unchanged).
    """
    segments = command_segments(command)
    for words in segments:
        if words and words[0] == "git":
            return words
    return first_segment_words(command)


def _combine_base(cd_dir: str | None, dash_c: str | None) -> str | None:
    """Combine a leading ``cd`` dir with ``git``'s ``-C`` dir.

    ``-C`` is applied on top of the ``cd`` dir: an absolute ``-C`` wins; a
    relative ``-C`` joins onto the ``cd`` dir. ``None`` when neither is present.
    """
    if dash_c is None:
        return cd_dir
    if cd_dir is None or Path(dash_c).is_absolute():
        return dash_c
    return str(Path(cd_dir) / dash_c)
