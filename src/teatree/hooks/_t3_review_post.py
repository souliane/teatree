r"""Body extraction for ``t3 review`` posting verbs (#2278/#32).

Split out of :mod:`teatree.hooks._command_parser` to keep that module under
the module-health LOC cap. This module owns one concern: given a ``t3 ...
review post-comment`` / ``post-draft-note`` command segment, locate the
posting verb and extract the published body — so the banned-terms / quote
gates scan it.

``t3 review post-comment REPO MR NOTE`` (and ``post-draft-note``) carry the
body as the positional ``NOTE``, not a ``--body``/``--message`` flag, so the
generic body-flag walkers found nothing. Two defects followed: a clean general
note's body was never scanned (a banned term in it slipped through, #2270) and
the inline ``--file`` anchor — a SOURCE path, not a body-file — was treated as
the body-file and the anchored source was scanned instead of the note (#2278).
:func:`_t3_review_post_verb_index` recognises the segment and
:func:`_t3_review_note_body` extracts the NOTE; the caller then suppresses the
generic body-file walker for the segment so the ``--file`` anchor is never read.

#32 added a ``--body-file`` source to ``review post-comment`` so large MR-thread
evidence posts through a scannable flag. Because the t3-review path suppresses
the generic body-file walker (to skip the ``--file`` anchor),
:func:`_t3_review_body_file_payload` reads the ``--body-file`` body HERE —
through the same :mod:`_body_file_resolution` machinery, fail-closed when
unreadable — while the ``--file`` anchor stays unscanned. The inline ``-m`` /
``--body`` body is still scanned by the generic body-flag walker that runs
before this module on every segment.

The leader is canonicalised up to the ``t3`` executable before recognition, so
an env-prefixed (``FOO=bar t3 …``) or path-form (``./t3``, ``/usr/local/bin/t3``)
invocation gets the same scanning as a bare ``t3`` leader, consistent with how
the surrounding parser normalises a segment's leader.
"""

from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final

from teatree.hooks._publish_detection import _ENV_ASSIGNMENT_RE

if TYPE_CHECKING:
    from teatree.hooks._body_file_resolution import BodyFileContext

# The ``t3 review`` posting verbs whose BODY is the positional ``NOTE``
# argument rather than a ``--body``/``--message`` flag.
_T3_REVIEW_POST_VERBS: Final[frozenset[str]] = frozenset({"post-comment", "post-draft-note"})

# Positional count consumed before the ``NOTE`` body of a ``t3 ... review
# <verb> REPO MR NOTE`` invocation: REPO and MR precede it.
_T3_REVIEW_NOTE_POSITIONAL_INDEX: Final[int] = 2

# Value-taking options on the ``t3 review`` post verbs whose next token is a
# VALUE, never a positional. Recognising them keeps the positional counter
# from miscounting a flag's value (``--line 3`` -> ``3``, a ``--body-file
# <path>`` path, a ``-m`` / ``--body`` text) as the NOTE body.
_T3_REVIEW_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"--file", "--line", "--evidence-json", "--body-file", "-m", "--body"},
)

# The ``--body-file`` flag on a ``t3 review`` post (#32): a FILE whose content
# is the published body, distinct from the ``--file`` diff anchor (a SOURCE path
# that must stay unscanned).
_T3_REVIEW_BODY_FILE_FLAG: Final[str] = "--body-file"

# The POSIX end-of-options marker. Typer requires it to pass a positional NOTE
# that itself starts with ``-``; everything after it is a positional.
_END_OF_OPTIONS: Final[str] = "--"


def _t3_leader_index(words: list[str]) -> int | None:
    """Return the index of the ``t3`` executable word, or ``None``.

    The leader is canonicalised up to the ``t3`` executable: leading
    ``KEY=value`` env assignments are skipped via the same permissive
    :data:`_publish_detection._ENV_ASSIGNMENT_RE` the publish-detection layer
    uses to classify the segment, and a path-form leader is reduced to its
    basename, so ``t3``, ``./t3``, ``/usr/local/bin/t3``, ``FOO=bar t3`` and
    ``foo=bar t3`` all resolve to the ``t3`` executable.
    """
    for i, word in enumerate(words):
        if _ENV_ASSIGNMENT_RE.fullmatch(word):
            continue
        return i if PurePosixPath(word).name == "t3" else None
    return None


def _t3_review_post_verb_index(words: list[str]) -> int | None:
    """Return the index of a ``t3 review`` posting verb word, or ``None``.

    The segment is a ``t3 review`` post iff its leader is the ``t3`` executable
    (env-prefixed and path-form leaders are canonicalised by
    :func:`_t3_leader_index`) and a ``review`` word is immediately followed by
    ``post-comment`` / ``post-draft-note``. The overlay word between ``t3`` and
    ``review`` is arbitrary, so ``review`` is located by scan rather than fixed
    position. Returns the index of the verb word
    (``post-comment``/``post-draft-note``).
    """
    leader = _t3_leader_index(words)
    if leader is None:
        return None
    for i in range(leader + 1, len(words) - 1):
        if words[i] == "review" and words[i + 1] in _T3_REVIEW_POST_VERBS:
            return i + 1
    return None


def _t3_review_note_body(words: list[str], raws: list[str], verb_index: int) -> str | None:
    """Return the positional ``NOTE`` body of a ``t3 review`` post, or ``None``.

    Walks the tokens AFTER the verb, skipping flags (and the value of a
    value-taking flag), and returns the third positional argument — the
    ``NOTE`` body of ``review <verb> REPO MR NOTE``. A standalone ``--``
    end-of-options marker is consumed; every token after it is a positional,
    so a dash-leading note (which Typer requires ``--`` to pass) is captured
    as the NOTE body rather than mistaken for a flag. ``None`` when fewer than
    three positionals are present. The body is passed through
    :func:`resolve_inline_body_value` (with the NOTE token's verbatim source
    span from ``raws``) so a LIVE ``$(cat <path>)`` / ``$VAR`` note resolves to
    its real content while a single-quoted INERT ``$(...)`` a multiline note
    merely MENTIONS is scanned verbatim rather than fail-closed (#1415);
    consistent with the inline body flags.
    """
    from teatree.hooks._body_file_resolution import resolve_inline_body_value  # noqa: PLC0415 — lazy import

    positionals: list[tuple[str, str]] = []
    i = verb_index + 1
    n = len(words)
    end_of_options = False
    while i < n:
        word = words[i]
        if not end_of_options and word == _END_OF_OPTIONS:
            end_of_options = True
            i += 1
            continue
        if not end_of_options and word.startswith("-"):
            if word in _T3_REVIEW_VALUE_FLAGS and "=" not in word:
                i += 2
                continue
            i += 1
            continue
        positionals.append((word, raws[i]))
        if len(positionals) > _T3_REVIEW_NOTE_POSITIONAL_INDEX:
            note_value, note_raw = positionals[_T3_REVIEW_NOTE_POSITIONAL_INDEX]
            return resolve_inline_body_value(note_value, None, note_raw)
        i += 1
    return None


def _t3_review_body_file_payload(words: list[str], payloads: list[str], ctx: "BodyFileContext") -> None:
    """Append the body referenced by a ``t3 review`` post's ``--body-file <path>`` (#32).

    Resolves ONLY the ``--body-file`` flag — never the ``--file`` diff anchor —
    through the shared :func:`_body_file_resolution._append_file_payload`, so a
    relative path resolves against the command's ``cd`` dir, an in-command
    heredoc/redirect body is paired, and an unreadable file fails closed (a
    public MR post whose body the gate cannot read must hard-block). Both the
    space-separated (``--body-file <path>``) and equals (``--body-file=<path>``)
    spellings are handled.
    """
    from teatree.hooks._body_file_resolution import _append_file_payload  # noqa: PLC0415 — deferred: call-time import
    from teatree.hooks._command_parser import attached_value  # noqa: PLC0415 — deferred: call-time import, kept lazy

    i = 0
    n = len(words)
    while i < n:
        word = words[i]
        if word == _T3_REVIEW_BODY_FILE_FLAG and i + 1 < n:
            _append_file_payload(words[i + 1], payloads, ctx, fail_closed=True)
            i += 2
            continue
        attached = attached_value(word, _T3_REVIEW_BODY_FILE_FLAG + "=")
        if attached is not None:
            _append_file_payload(attached, payloads, ctx, fail_closed=True)
        i += 1


def append_t3_review_note_payload(
    words: list[str], raws: list[str], payloads: list[str], ctx: "BodyFileContext"
) -> bool:
    """Append a ``t3 review`` post's body and report handling.

    Returns ``True`` when ``words`` is a ``t3 review post-comment`` /
    ``post-draft-note`` segment — the caller then SKIPS the generic body-file
    walker so the segment's inline ``--file`` anchor is never scanned as the
    published body (#2278/#2270). The published body is appended to ``payloads``
    from whichever source carries it: the positional ``NOTE`` (via
    :func:`_t3_review_note_body`) and the ``--body-file`` content (via
    :func:`_t3_review_body_file_payload`, #32). The inline ``-m`` / ``--body``
    body is already covered by the generic body-flag walker the caller runs
    before this on every segment. ``raws`` carries each WORD token's verbatim
    source span (parallel to ``words``) so the NOTE resolver can tell a
    single-quoted INERT ``$(...)`` from a live one. ``ctx`` is the body-file
    resolution context (heredoc map, repo-dir base) the ``--body-file`` reader
    needs. Returns ``False`` for a non-review segment, leaving ``payloads``
    untouched.
    """
    verb_index = _t3_review_post_verb_index(words)
    if verb_index is None:
        return False
    note = _t3_review_note_body(words, raws, verb_index)
    if note is not None:
        payloads.append(note)
    _t3_review_body_file_payload(words, payloads, ctx)
    return True
