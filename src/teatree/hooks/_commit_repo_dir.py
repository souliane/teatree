r"""Resolve the dir whose repo a ``git`` command's commit LANDS in.

Split out of :mod:`teatree.hooks.publish_surface` to keep that module under
the module-health LOC cap. This module owns one concern: from a Bash command
string, statically resolve the working directory ``git`` would use, mirroring
``git``'s documented global-flag semantics so the banned-terms carve-out
decides privacy from the repo the commit ACTUALLY lands in.

``git`` selects a commit's repo as: the ``--git-dir``/``$GIT_DIR`` repo if
specified, else the repo discovered from the effective working directory,
which ``-C <dir>`` changes. Repeated ``-C`` is CUMULATIVE (each non-absolute
``-C <path>`` is relative to the preceding one; an absolute ``-C`` resets);
repeated ``--git-dir`` is last-wins. ``--work-tree`` only sets the working
tree and NEVER selects the repo, so it is excluded -- a ``--git-dir <PUBLIC>
--work-tree <PRIVATE>`` commit lands in the PUBLIC repo.

Fail closed: a ``-C`` value the gate cannot resolve statically (e.g. a
substitution marker) yields :data:`UNRESOLVABLE_REPO_DIR`, and the carve-out
must then refuse to downgrade rather than guess a target.
"""

from pathlib import Path
from typing import Final

from teatree.hooks._command_parser import first_segment_words
from teatree.hooks._gh_glab_hiding import token_has_substitution_marker

# Returned by ``effective_repo_dir`` when a ``-C`` value cannot be resolved
# statically (e.g. it carries a substitution marker). The commit carve-out
# treats this as an UNKNOWN target and refuses to downgrade -- fail closed,
# never leak onto a target we cannot pin down.
UNRESOLVABLE_REPO_DIR: Final[str] = "\x00teatree-unresolvable-repo-dir\x00"


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


def effective_repo_dir(command: str) -> str | None:
    """Return the dir whose repo a ``git`` command's commit LANDS in, or ``None``.

    Repeated ``-C`` flags are CUMULATIVE, not last-wins: each subsequent
    non-absolute ``-C <path>`` joins onto the preceding one and an absolute
    ``-C <path>`` resets the accumulator (:func:`_cumulative_dash_c`), matching
    ``git``'s documented behaviour. Repeated ``--git-dir`` IS last-wins.
    ``--work-tree`` never selects the repo and is excluded.

    Resolution: ``--git-dir`` (last-wins) if present, resolved relative to the
    accumulated ``-C`` dir when relative; else the accumulated ``-C`` dir.
    ``None`` when neither flag is present, so the caller falls back to the
    ambient cwd for a plain ``git commit``. The fail-closed sentinel
    :data:`UNRESOLVABLE_REPO_DIR` is returned when a ``-C`` value cannot be
    statically resolved, so the carve-out never downgrades onto an unknown
    target.
    """
    words = first_segment_words(command)
    dash_c = _cumulative_dash_c(words)
    if dash_c == UNRESOLVABLE_REPO_DIR:
        return UNRESOLVABLE_REPO_DIR
    git_dir = _last_flag_value(words, "--git-dir")
    if git_dir is not None:
        if dash_c is not None and not Path(git_dir).is_absolute():
            return str(Path(dash_c) / git_dir)
        return git_dir
    return dash_c
