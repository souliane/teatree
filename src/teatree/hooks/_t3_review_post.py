r"""Positional-NOTE body extraction for ``t3 review`` posting verbs (#2278).

Split out of :mod:`teatree.hooks._command_parser` to keep that module under
the module-health LOC cap. This module owns one concern: given a ``t3 ...
review post-comment`` / ``post-draft-note`` command segment, locate the
posting verb and extract the positional ``NOTE`` argument — the published
body — so the banned-terms / quote gates scan it.

``t3 review post-comment REPO MR NOTE`` (and ``post-draft-note``) carry the
body as the positional ``NOTE``, not a ``--body``/``--message`` flag, so the
generic body-flag walkers found nothing. Two defects followed: a clean general
note's body was never scanned (a banned term in it slipped through, #2270) and
the inline ``--file`` anchor — a SOURCE path, not a body-file — was treated as
the body-file and the anchored source was scanned instead of the note (#2278).
:func:`t3_review_post_verb_index` recognises the segment and
:func:`t3_review_note_body` extracts the NOTE; the caller then suppresses the
body-file walker for the segment so the anchor is never read.
"""

from typing import Final

# The ``t3 review`` posting verbs whose BODY is the positional ``NOTE``
# argument rather than a ``--body``/``--message`` flag.
_T3_REVIEW_POST_VERBS: Final[frozenset[str]] = frozenset({"post-comment", "post-draft-note"})

# Positional count consumed before the ``NOTE`` body of a ``t3 ... review
# <verb> REPO MR NOTE`` invocation: REPO and MR precede it.
_T3_REVIEW_NOTE_POSITIONAL_INDEX: Final[int] = 2

# Value-taking options on the ``t3 review`` post verbs whose next token is a
# VALUE, never a positional. Recognising them keeps the positional counter
# from miscounting a flag's value (``--line 3`` -> ``3``) as the NOTE body.
_T3_REVIEW_VALUE_FLAGS: Final[frozenset[str]] = frozenset({"--file", "--line", "--evidence-json"})


def _t3_review_post_verb_index(words: list[str]) -> int | None:
    """Return the index of a ``t3 review`` posting verb word, or ``None``.

    The segment is a ``t3 review`` post iff its leader is ``t3`` and a
    ``review`` word is immediately followed by ``post-comment`` /
    ``post-draft-note``. The overlay word between ``t3`` and ``review`` is
    arbitrary, so ``review`` is located by scan rather than fixed position.
    Returns the index of the verb word (``post-comment``/``post-draft-note``).
    """
    if not words or words[0] != "t3":
        return None
    for i in range(1, len(words) - 1):
        if words[i] == "review" and words[i + 1] in _T3_REVIEW_POST_VERBS:
            return i + 1
    return None


def _t3_review_note_body(words: list[str], verb_index: int) -> str | None:
    """Return the positional ``NOTE`` body of a ``t3 review`` post, or ``None``.

    Walks the tokens AFTER the verb, skipping flags (and the value of a
    value-taking flag), and returns the third positional argument — the
    ``NOTE`` body of ``review <verb> REPO MR NOTE``. ``None`` when fewer than
    three positionals are present (a malformed invocation the CLI itself
    rejects). The body is passed through :func:`resolve_inline_body_value` so a
    ``$(cat <path>)`` / ``$VAR`` note resolves to its real content, consistent
    with the inline body flags; an unresolvable indirection fails closed.
    """
    from teatree.hooks._body_file_resolution import resolve_inline_body_value  # noqa: PLC0415

    positionals: list[str] = []
    i = verb_index + 1
    n = len(words)
    while i < n:
        word = words[i]
        if word.startswith("-"):
            if word in _T3_REVIEW_VALUE_FLAGS and "=" not in word:
                i += 2
                continue
            i += 1
            continue
        positionals.append(word)
        if len(positionals) > _T3_REVIEW_NOTE_POSITIONAL_INDEX:
            return resolve_inline_body_value(positionals[_T3_REVIEW_NOTE_POSITIONAL_INDEX], None)
        i += 1
    return None


def append_t3_review_note_payload(words: list[str], payloads: list[str]) -> bool:
    """Append a ``t3 review`` post's positional NOTE body and report handling.

    Returns ``True`` when ``words`` is a ``t3 review post-comment`` /
    ``post-draft-note`` segment — the caller then SKIPS the generic body-flag
    and body-file walkers so the segment's inline ``--file`` anchor is never
    scanned as the published body (#2278/#2270). The extracted NOTE (when
    present) is appended to ``payloads``. Returns ``False`` for a non-review
    segment, leaving ``payloads`` untouched.
    """
    verb_index = _t3_review_post_verb_index(words)
    if verb_index is None:
        return False
    note = _t3_review_note_body(words, verb_index)
    if note is not None:
        payloads.append(note)
    return True
