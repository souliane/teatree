r"""Token-aware publish/commit/api detection for the pre-publish gates (#1672).

Split out of :mod:`teatree.hooks._command_parser` to keep that module under the
project's per-file LOC ceiling. This module owns ONE concern: decide, by WORD
position rather than contiguous substring, whether a Bash command segment is a
publish surface the gates must scan.

The original detection (:data:`_command_parser._BASH_PUBLISH_SUBSTRINGS`) matches
CONTIGUOUS substrings (``gh api ``, ``git commit -m``). An interspersed
persistent flag breaks contiguity, so a real publish slipped detection unseen:

- ``gh --hostname H api ...`` / ``gh -X POST api ...`` -- a persistent flag
    before the ``api`` sub-command (:func:`segment_is_api_call`);
- ``git -C <dir> commit -m ...`` / ``git --git-dir=x commit --message ...`` --
    a value-taking global flag before the ``commit`` verb
    (:func:`segment_is_git_commit_publish`); and
- ``sh -c "gh ... --body X"`` / ``eval`` / ``ssh host gh`` / ``xargs gh`` -- a
    forge call HIDDEN inside an interpreter argument the body walkers cannot
    descend into (:func:`segment_is_opaque_forge_transport`), which the gates
    fail closed on rather than scan an unreachable body.

Position-aware matching is robust to flag ordering WITHOUT enumerating every
persistent flag -- the closed inversion the anti-whack-a-mole doctrine requires.
"""

import re
from typing import Final

from teatree.hooks._gh_glab_hiding import token_has_substitution_marker
from teatree.hooks._shell_lexer import TokenKind, split_commands, tokenize

_ENV_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")

# Value-taking global ``git`` flags that sit BEFORE the ``commit`` verb
# (``git -C <dir> commit``, ``--git-dir``, ``--work-tree``). The token-aware walk
# skips them (flag + value, plus their ``=`` forms) so the ``commit`` verb is
# reached. Mirrors ``publish_surface._GIT_GLOBAL_DIR_FLAGS``.
_GIT_GLOBAL_DIR_FLAGS: Final[frozenset[str]] = frozenset({"-C", "--git-dir", "--work-tree"})

# Message-bearing flags that make a ``git commit`` a publish surface (its body
# lands in public history): ``-m`` / ``--message`` / ``-F`` / ``--file`` -- the
# same set the substring catalogue covers, now reached token-aware.
_GIT_COMMIT_BODY_FLAGS: Final[frozenset[str]] = frozenset({"-m", "--message", "-F", "--file"})
_GIT_COMMIT_BODY_ATTACHED: Final[tuple[str, ...]] = ("-m", "-F", "--message=", "--file=")

# Forge-tool command words the body-extracting walkers can parse a body out of.
# A segment whose LEADING executable (after cd/env) is one of these is parseable;
# a forge token appearing only NESTED (a ``sh -c "gh ... --body X"`` interpreter
# arg, an ``eval``/``ssh``/``xargs`` wrapper) is an OPAQUE forge transport the
# walkers cannot reach -- so the body the post carries is unscannable.
_PARSEABLE_FORGE_LEADERS: Final[frozenset[str]] = frozenset({"gh", "glab", "git", "curl"})

# Forge-tool markers detected as a SUBSTRING of any token, so a forge call
# hidden inside a quoted interpreter argument is recognised as a transport.
_FORGE_TOOL_MARKERS: Final[tuple[str, ...]] = ("gh", "glab", "curl")

# Title / commit-subject flags (#1544). A title (``gh``/``glab`` ``--title``)
# or git-commit subject is a forge surface distinct from the description body.
_TITLE_LONG_FLAG: Final[str] = "--title"
_TITLE_SHORT_FLAG: Final[str] = "-t"
_GIT_COMMIT_MESSAGE_FLAGS: Final[frozenset[str]] = frozenset({"-m", "--message"})


def _attached_value(token: str, prefix: str) -> str | None:
    """Return the attached value of ``-X<value>`` / ``-X=<value>``, if any."""
    if token.startswith(prefix) and len(token) > len(prefix):
        return token[len(prefix) :].removeprefix("=")
    return None


def segment_word_lists(command: str) -> list[list[str]]:
    """Return the WORD-value list of every top-level command segment.

    Leading inline ``KEY=value`` env assignments are stripped so a publish verb
    behind an env prefix is still found. Mirrors
    :func:`_gh_glab_hiding.command_segments`.
    """
    segments: list[list[str]] = []
    for segment in split_commands(tokenize(command)):
        words = [tok.value for tok in segment if tok.kind is TokenKind.WORD]
        while words and _ENV_ASSIGNMENT_RE.fullmatch(words[0]):
            words = words[1:]
        if words:
            segments.append(words)
    return segments


def segment_is_api_call(words: list[str]) -> bool:
    """Return True iff ``words`` is a ``gh``/``glab`` raw-REST call.

    A ``gh``/``glab`` segment is raw REST iff the ``api`` sub-command WORD
    appears anywhere after the tool word, regardless of any interspersed
    persistent flag (``--hostname H``, ``-R repo``, ``-X POST``) or stray token.
    This catches ``gh --hostname github.com api ...`` and ``gh -X POST api ...``
    the contiguous ``gh api `` substring missed. Matching the bare ``api`` WORD
    is robust to flag ordering without enumerating every persistent flag; a
    quoted flag VALUE that merely contains the text ``api`` is a single distinct
    token, so it does not match.
    """
    return bool(words) and words[0] in {"gh", "glab"} and "api" in words[1:]


def segment_is_git_commit_publish(words: list[str]) -> bool:
    """Return True iff ``words`` is a ``git [global-flags] commit`` with a body flag.

    A leading ``cd``/``pushd`` navigation prefix and the value-taking ``git``
    global flags (``-C <dir>``, ``--git-dir``, ``--work-tree``, plus ``=`` forms)
    are skipped so ``git -C <dir> commit -m ...`` and
    ``git --git-dir=x commit --message ...`` reach the ``commit`` verb -- the
    contiguous ``git commit -m`` substring broke on the interspersed flag. A
    commit publishes (to public history) only when it carries an inline message /
    file flag; a flagless ``git commit`` is interactive and out of scope here.
    """
    rest = _strip_cd_prefix(words)
    if not rest or rest[0] != "git":
        return False
    i = 1
    while i < len(rest):
        word = rest[i]
        if word in _GIT_GLOBAL_DIR_FLAGS:
            i += 2
            continue
        if any(word.startswith(flag + "=") for flag in _GIT_GLOBAL_DIR_FLAGS):
            i += 1
            continue
        break
    if i >= len(rest) or rest[i] != "commit":
        return False
    return any(_token_is_commit_body_flag(tok) for tok in rest[i + 1 :])


def _token_is_commit_body_flag(token: str) -> bool:
    return token in _GIT_COMMIT_BODY_FLAGS or any(
        token.startswith(prefix) and len(token) > len(prefix) for prefix in _GIT_COMMIT_BODY_ATTACHED
    )


def _strip_cd_prefix(words: list[str]) -> list[str]:
    rest = words
    while rest and rest[0] in {"cd", "pushd"} and len(rest) >= 2:  # noqa: PLR2004
        rest = rest[2:]
    return rest


def _strip_cd_env_prefix(words: list[str]) -> list[str]:
    rest = words
    while rest:
        if _ENV_ASSIGNMENT_RE.fullmatch(rest[0]):
            rest = rest[1:]
            continue
        if rest[0] in {"cd", "pushd"} and len(rest) >= 2:  # noqa: PLR2004
            rest = rest[2:]
            continue
        break
    return rest


def segment_is_opaque_forge_transport(words: list[str]) -> bool:
    """Return True iff ``words`` carries a forge call the body walkers cannot parse.

    A segment is an OPAQUE forge transport when a ``gh``/``glab``/``curl`` token
    (or a command/process-substitution marker) is present but the segment's
    LEADING executable is NOT one of the parseable forge tools -- i.e. the forge
    invocation hides inside an interpreter / wrapper argument (``sh -c "gh ...
    --body X"``, ``eval "..."``, ``ssh host gh ...``, ``xargs gh ...``). The body
    the post carries then sits inside an opaque argument the per-command walkers
    never descend into, so its content (a banned term, a bare ref) cannot be
    scanned. The destination-aware gates inject the fail-closed sentinel for such
    a segment so the unscannable post HARD-BLOCKS rather than slips through
    unread -- mirroring the prove-pure-or-fail-closed inversion.

    A plain ``gh``/``glab``/``git``/``curl`` invocation at ``words[0]`` is NOT
    opaque (the walkers parse its body); a forge-free segment (``git push``,
    ``echo done``) is NOT a transport.
    """
    rest = _strip_cd_env_prefix(words)
    if not rest or rest[0] in _PARSEABLE_FORGE_LEADERS:
        return False
    carries_forge = any(any(marker in token for marker in _FORGE_TOOL_MARKERS) for token in rest)
    carries_substitution = any(token_has_substitution_marker(token) for token in rest)
    return carries_forge or carries_substitution


def command_has_token_aware_publish_surface(command: str) -> bool:
    """Return True iff any segment is a token-aware ``api`` / ``git commit`` publish.

    The position-aware complement of the contiguous-substring catalogue, used by
    :func:`_command_parser.is_publish_command` to catch the interspersed-flag
    spellings the substring matcher misses.
    """
    return any(
        segment_is_api_call(words) or segment_is_git_commit_publish(words) for words in segment_word_lists(command)
    )


def command_has_opaque_forge_transport(command: str) -> bool:
    """Return True iff any segment hides a forge call in an opaque interpreter arg."""
    return any(segment_is_opaque_forge_transport(words) for words in segment_word_lists(command))


def _forge_title_value(words: list[str]) -> str | None:
    """Return the ``--title``/``-t`` value of a ``gh``/``glab`` segment.

    Handles space-separated (``--title "x"``), equals (``--title=x``), and
    attached short (``-tx``) forms. ``None`` when the segment carries no title
    flag.
    """
    i = 0
    n = len(words)
    while i < n:
        word = words[i]
        if word in {_TITLE_LONG_FLAG, _TITLE_SHORT_FLAG} and i + 1 < n:
            return words[i + 1]
        attached = _attached_value(word, _TITLE_LONG_FLAG + "=")
        if attached is not None:
            return attached
        if word != _TITLE_SHORT_FLAG:
            attached = _attached_value(word, _TITLE_SHORT_FLAG)
            if attached is not None:
                return attached
        i += 1
    return None


def _git_commit_subject(words: list[str]) -> str | None:
    """Return the SUBJECT line of a ``git commit`` segment.

    The subject is the first physical line of the first ``-m``/``--message``
    value (later ``-m`` values are body paragraphs). ``None`` when the segment
    carries no inline message.
    """
    i = 0
    n = len(words)
    while i < n:
        word = words[i]
        if word in _GIT_COMMIT_MESSAGE_FLAGS and i + 1 < n:
            return words[i + 1].split("\n", 1)[0]
        attached = _attached_value(word, "--message=")
        if attached is not None:
            return attached.split("\n", 1)[0]
        attached = _attached_value(word, "-m")
        if attached is not None:
            return attached.split("\n", 1)[0]
        i += 1
    return None


def extract_title_fragments(command: str) -> list[str]:
    """Return the TITLE / commit-SUBJECT fragments the command publishes.

    A title (``gh``/``glab`` ``--title``) or git-commit subject is a forge
    surface distinct from a description body: the forge auto-links a trailing
    ``(#NNNN)``/``(!NNNN)`` reference there. A gate that wants to treat that
    conventional suffix differently from a body reads these fragments instead of
    the flattened body blob (#1544).
    """
    fragments: list[str] = []
    for words in segment_word_lists(command):
        if words[0] in {"gh", "glab"}:
            title = _forge_title_value(words)
            if title is not None:
                fragments.append(title)
        elif words[0] == "git":
            subject = _git_commit_subject(words)
            if subject is not None:
                fragments.append(subject)
    return fragments
