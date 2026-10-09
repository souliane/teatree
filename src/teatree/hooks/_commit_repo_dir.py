r"""Resolve the dir whose repo a ``git`` command's commit LANDS in.

Split out of :mod:`teatree.hooks.publish_surface` to keep that module under
the module-health LOC cap. This module owns one concern: from a Bash command
string, statically resolve the working directory a command runs in, mirroring
``git``'s documented global-flag semantics so the banned-terms carve-out
decides privacy from the repo the commit ACTUALLY lands in.
:func:`segment_cwds` answers the same question PER top-level segment, so the
publish gates classify each segment against the dir bash would run IT in rather
than one dir applied to the whole command.

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

Every dir token is read the way the SHELL hands it to ``git``: a leading
``~``/``~user`` is expanded (:func:`_shell_path`), because a tilde kept
verbatim parses as a RELATIVE path and every anchor and walk below it is then
computed against the wrong base.

Fail closed: a ``-C`` value the gate cannot resolve statically (e.g. a
substitution marker), or a leading ``cd`` that lands on no real directory,
both yield :data:`UNRESOLVABLE_REPO_DIR`, and the carve-out must then refuse to
downgrade rather than guess a target. A ``cd`` that goes nowhere is not a soft
case: the ``cd`` itself fails, so the command commits nowhere, while walking UP
from the nonexistent path resolves the AMBIENT session repo -- a wrong subject
for the privacy decision that is indistinguishable from a right one.

:func:`git_root_for_dir` walks UP from a resolved dir to the nearest
enclosing ``.git`` so a commit run from a SUBDIR of a worktree still resolves
to the worktree's repo (and so the carve-out can tell a genuinely-unresolvable
commit -- no enclosing repo anywhere -- from a resolvable one). The walk is
only ever entered from a dir that exists, so it cannot manufacture that wrong
subject.
"""

from pathlib import Path
from typing import Final

from teatree.hooks._command_parser import first_segment_words
from teatree.hooks._gh_glab_hiding import command_segments, token_has_substitution_marker
from teatree.hooks._shell_lexer import TokenKind, tokenize

# Returned by ``effective_repo_dir`` when a ``-C`` value cannot be resolved
# statically (e.g. it carries a substitution marker). The commit carve-out
# treats this as an UNKNOWN target and refuses to downgrade -- fail closed,
# never leak onto a target we cannot pin down.
UNRESOLVABLE_REPO_DIR: Final[str] = "\x00teatree-unresolvable-repo-dir\x00"

# A ``cd <path>`` / ``pushd <path>`` navigation segment needs the verb plus
# its single path argument.
_NAV_WITH_PATH_WORD_COUNT: Final[int] = 2
NAVIGATION_VERBS: Final[frozenset[str]] = frozenset({"cd", "pushd"})

# Every verb that MOVES the shell's working directory. Wider than ``NAVIGATION_VERBS``:
# ``popd`` names no path, so it moves the cwd to somewhere this parse cannot know.
_CWD_MUTATING_VERBS: Final[frozenset[str]] = NAVIGATION_VERBS | {"popd"}


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
        path = _shell_path(value)
        accumulator = path if path.is_absolute() or accumulator is None else accumulator / path
    return str(accumulator) if accumulator is not None else None


def _shell_path(value: str) -> Path:
    """Return the path ``value`` denotes AFTER the shell's ``~`` expansion.

    The shell expands a leading ``~``/``~user`` before ``cd``/``git`` ever see
    the argument, so a static parse that keeps the tilde verbatim reads
    ``~/repo`` as a RELATIVE path and every downstream anchor/walk is then
    computed against the wrong base. Expanding here is what makes the parse
    agree with what the command actually does.
    """
    return Path(value).expanduser()


def anchored_dir(parsed: str, cwd: Path | None) -> Path:
    """Return ``parsed`` as an absolute dir: ``~`` expanded, a relative value anchored on ``cwd``.

    The one place a parsed dir token becomes a filesystem path, so the ``cd``
    resolvers in this module and the cold-hook siblings that reuse them
    (``hooks/scripts/coverage_gate.py``) cannot drift on ``~`` handling or on
    which base a relative value hangs off. ``cwd`` is the AMBIENT harness cwd,
    never the cold hook's process cwd.
    """
    path = _shell_path(parsed)
    return path if path.is_absolute() or cwd is None else cwd / path


def leading_cd_dir(command: str) -> str | None:
    """Return the working dir a leading ``cd``/``pushd`` chain resolves to, or ``None``.

    Walks the LEADING navigation segments of ``command`` (``cd <path>`` /
    ``pushd <path>`` separated by ``&&``/``;``/...), stopping at the first
    non-navigation segment (the ``git commit``). Each subsequent non-absolute
    path joins onto the preceding one; an absolute path resets the accumulator,
    mirroring shell semantics -- including the shell's ``~`` expansion
    (:func:`_shell_path`), so ``cd ~/repo`` yields an ABSOLUTE dir rather than a
    relative ``~/repo`` a caller would anchor on the ambient cwd. ``None`` when
    no leading ``cd``/``pushd`` is present, so the caller falls back to the
    ambient cwd.
    """
    accumulator: Path | None = None
    for words in command_segments(command):
        if len(words) < _NAV_WITH_PATH_WORD_COUNT or words[0] not in NAVIGATION_VERBS:
            break
        path = _shell_path(words[1])
        accumulator = path if path.is_absolute() or accumulator is None else accumulator / path
    return str(accumulator) if accumulator is not None else None


def leading_cd_target(command: str, cwd: Path | None) -> Path | str | None:
    """Return the dir a leading ``cd``/``pushd`` chain LANDS in, three-valued.

    The three states a caller must be able to tell apart -- the distinction
    :func:`git_root_for_dir`'s walk-up silently erases:

    - ``None`` -- the command carries no leading ``cd``/``pushd``, so the caller
        falls back to the ambient ``cwd``.
    - :data:`UNRESOLVABLE_REPO_DIR` -- the command NAMES a dir that is not a real
        directory (an unexpanded ``$VAR``, a typo, a stale path). The ``cd``
        itself would fail, so the command runs nowhere; anchoring the
        nonexistent path on ``cwd`` and walking UP lands on whatever repo the
        session happens to sit in -- a confident wrong answer that reads exactly
        like a correct one. Fail closed instead, the same sentinel
        :func:`_cumulative_dash_c` already returns for an unpinnable ``-C``.
    - an absolute :class:`~pathlib.Path` -- the dir the ``cd`` lands in.
    """
    cd_dir = leading_cd_dir(command)
    if cd_dir is None:
        return None
    target = anchored_dir(cd_dir, cwd)
    return target if target.is_dir() else UNRESOLVABLE_REPO_DIR


def _command_opens_subshell(command: str) -> bool:
    """Return True iff ``command`` opens a ``( ... )`` group.

    The lexer emits the OPENER as a separator but glues the CLOSER onto its
    neighbouring word, so a subshell's extent is not reconstructable -- and with it,
    whether a ``cd`` inside the group survives past it. Bash restores the cwd when the
    subshell exits, so a tracker that cannot see the exit would carry the inner ``cd``
    onto the segments that follow and classify them against the wrong repo.
    """
    return any(token.kind is TokenKind.OP and token.value == "(" for token in tokenize(command))


def _navigated_dir(words: list[str], running: Path | None) -> Path | None:
    """Return the dir a cwd-mutating segment lands in, or ``None`` when unprovable."""
    if len(words) != _NAV_WITH_PATH_WORD_COUNT or words[0] not in NAVIGATION_VERBS:
        return None
    if running is None and not _shell_path(words[1]).is_absolute():
        return None
    target = anchored_dir(words[1], running)
    return target if target.is_dir() else None


def segment_cwds(command: str, cwd: Path | None) -> list[Path | None]:
    """Return the dir EACH top-level segment runs in, aligned with :func:`command_segments`.

    The cwd MOVES as bash moves it: every ``cd``/``pushd`` segment re-points it for the
    segments that FOLLOW. Resolving one dir for the whole command instead -- from its
    leading ``cd`` or from the ambient hook cwd -- classifies every later publish against
    the FIRST dir, so a second ``cd`` into a public clone posts under the first one's
    verdict and an obscured public post hides behind a leading non-public segment.

    ``None`` is the fail-closed answer for a dir this parse cannot prove: a ``cd`` onto no
    real directory (the ``cd`` fails, so the command publishes nowhere), a bare
    ``cd``/``popd``/``cd -``, a relative ``cd`` with no base to anchor on, or ANY navigation
    once a subshell has opened (:func:`_command_opens_subshell`). Both consumers -- the
    leak-gate visibility scope and the private-post carve-out -- already read ``None`` as
    "no provable destination" and refuse to skip on it.
    """
    scoped = _command_opens_subshell(command)
    running = cwd
    cwds: list[Path | None] = []
    for words in command_segments(command):
        cwds.append(running)
        if words[0] in _CWD_MUTATING_VERBS:
            running = None if scoped else _navigated_dir(words, running)
    return cwds


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


def resolve_commit_dir(command: str, cwd: Path | None) -> Path | str | None:
    """Resolve the dir whose repo a ``git`` command's commit LANDS in.

    Combines the command-only parse (:func:`effective_repo_dir` -- a leading
    ``cd``/``pushd`` prefix plus ``-C``/``--git-dir``, never ``--work-tree``)
    with the AMBIENT hook ``cwd``: a RELATIVE parsed dir (``git -C
    ../worktree``, the form a sub-agent's command takes when the harness has
    reset the hook ``cwd`` to a sibling repo) is anchored on ``cwd`` so it
    resolves against the dir the agent actually ran in, NOT the cold hook's
    process cwd. Anchoring on the process cwd (the prior behaviour, an implicit
    ``Path(repo_dir)`` with no base) silently mis-resolved the worktree -- a
    relative public target then resolved to no repo and FAIL-OPENED the
    carve-out (a banned-term leak to the public repo), and a relative private
    target resolved by accident only when the process cwd happened to match.

    A leading ``cd`` that lands on NO REAL DIRECTORY is the fail-closed
    sentinel, never a walk-up (:func:`leading_cd_target`): the ``cd`` itself
    fails, so the command commits nowhere, while anchoring the nonexistent path
    on ``cwd`` and letting :func:`git_root_for_dir` walk UP resolves the AMBIENT
    session repo and hands the carve-out a wrong subject that is
    indistinguishable from a right one. The ``~``-prefixed form used to be
    exactly such a path. ``-C``/``--git-dir`` keep their established
    existence-free semantics -- a ``-C`` dir inside no repo at all is the
    documented fail-OPEN case, and a ``--git-dir`` value is normalised as a pure
    path -- so the guard is scoped to the navigation prefix that produced the
    walk-up.

    Returns:
    - :data:`UNRESOLVABLE_REPO_DIR` when a leading ``cd`` lands on no real
        directory, or when ``effective_repo_dir`` could not pin the ``-C`` value
        statically (a substitution marker) -- the caller must then NOT downgrade
        (fail closed).
    - an absolute :class:`~pathlib.Path` of the resolved commit dir when the
        command named one (``cd``/``-C``/``--git-dir``), anchored on ``cwd``
        when the parsed dir is relative and ``cwd`` is given.
    - ``cwd`` itself for a plain ``git commit`` that named no dir (it lands in
        the ambient cwd's repo), or ``None`` when neither resolves -- the
        caller reads ``None`` as a genuinely-unresolvable LOCAL commit.
    """
    if leading_cd_target(command, cwd) == UNRESOLVABLE_REPO_DIR:
        return UNRESOLVABLE_REPO_DIR
    repo_dir = effective_repo_dir(command)
    if repo_dir == UNRESOLVABLE_REPO_DIR:
        return UNRESOLVABLE_REPO_DIR
    if repo_dir is None:
        return cwd
    return anchored_dir(repo_dir, cwd)


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
        expanded = _shell_path(git_dir)
        if base is not None and not expanded.is_absolute():
            return str(Path(base) / expanded)
        return str(expanded)
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
