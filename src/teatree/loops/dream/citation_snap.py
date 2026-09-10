"""Re-EXTRACT a near-miss citation to the snippet's own bytes, or say why not (#4671, #4716).

The dream distiller reproduces a quote from memory and drops an article or re-punctuates a
clause, so a genuinely grounded rule dies on the strict substring test in
:func:`~teatree.loops.dream.engine.check_grounding` — nine in one observed pass. Rather
than loosen that test, this module locates the window the citation nearly quotes and hands
back the SNIPPET's own text, so what the ledger records is verbatim by construction.

That re-extraction is exactly why locating cannot be admitting. ``SequenceMatcher.ratio()``
is character-level and blind to polarity: on one window a ``SKIPS`` -> ``RUNS`` inversion
scores 0.9818 and an inserted ``never`` 0.9674, while the observed dropped-article
paraphrase the snap exists to rescue scores 0.9149 — so every threshold admitting the
paraphrase admits both inversions, and re-extraction would leave a ledger row whose verbatim
quote asserts the OPPOSITE of its own rule. The ratio therefore only nominates a window; the
token delta between the citation and that window decides admission.
``tests/teatree_loops/dream/test_citation_snap.py`` measures the three digits above, so they
cannot drift back into an anecdote nobody can reproduce.
"""

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

#: How close a citation must be to a real snippet window before that window is a candidate
#: at all. The ratio LOCATES the window; admission is the token-delta rule below, because
#: the ratio is character-level and cannot see polarity (#4716).
_SNAP_MIN_RATIO = 0.90
#: Below this many characters a citation cannot identify one window rather than another, so
#: a short generic fragment is rejected instead of snapped to an arbitrary match.
_SNAP_MIN_CITATION_CHARS = 40
#: Where alignment anchors are taken from within the citation. A near-miss diverges from
#: the snippet somewhere, so anchoring at several offsets keeps one damaged region from
#: hiding an otherwise exact quote.
_SNAP_ANCHOR_OFFSETS = (0.0, 0.25, 0.5, 0.75)
#: An anchor shorter than this matches too many places to locate a window.
_SNAP_ANCHOR_CHARS = 24
#: How far past the located window the token delta is read. The window's own ends come from
#: guessing its length from the citation's, so a difference there is this code's artefact.
_SNAP_WINDOW_MARGIN_CHARS = 64
#: The only token differences a snap may forgive: articles, which assert nothing on their
#: own. Connectives and prepositions are NOT meaning-free — `and` -> `or` turns a rule's
#: conjunction into a disjunction and `on` -> `at` restates the relation, so a snap over one
#: records a quote asserting something the cited snippet does not (#4716).
_SNAP_ADMISSIBLE_DELTA = frozenset({"a", "an", "the"})
#: Tokens that flip a rule's polarity, so the truncated-edge carve-out may never excuse one.
_SNAP_NEGATORS = frozenset({"not", "never", "no", "none", "nor", "cannot", "without"})
#: Suffixes whose removal negates the word they are cut from, so `harmless` -> `harm` wears a
#: tail cut's shape. Named rather than failed-closed because a tail cut is the common
#: near-miss the snap exists to rescue, unlike the head (see :func:`_is_truncated_edge`).
_SNAP_NEGATING_SUFFIXES = ("less",)
#: How much of a refused delta the rejection message spells out before eliding.
_SNAP_RENDER_MAX_TOKENS = 8
_SNAP_TOKEN_RE = re.compile(r"[0-9a-z]+(?:'[0-9a-z]+)*")


@dataclass(frozen=True, slots=True)
class CitationSnap:
    """What the snap made of a citation the strict substring test could not find.

    ``window`` is the snippet's own text to record, ``None`` when nothing was admitted.
    ``composed`` names the token delta of the closest window that WAS located and then
    refused, so the rejection can say which word the model changed rather than "not found".
    """

    window: str | None
    composed: tuple[str, ...]


def snap_citation(citation: str, snippets: Sequence[str]) -> CitationSnap:
    """The snippet's OWN text for the window *citation* nearly quotes, else why not.

    A located window is admitted only when every token differing from the citation is an
    article (:data:`_SNAP_ADMISSIBLE_DELTA`); a refused one is skipped rather than returned,
    so a later clean window can still admit.

    Alignment is anchored rather than searched: scoring every window of a long snippet is
    quadratic, and the tail this runs in has no time to spare.
    """
    if len(citation) < _SNAP_MIN_CITATION_CHARS:
        return CitationSnap(None, ())
    citation_tokens = _tokens(citation)
    best: str | None = None
    best_ratio = _SNAP_MIN_RATIO
    composed: tuple[str, ...] = ()
    composed_ratio = _SNAP_MIN_RATIO
    for snippet in snippets:
        for start in _anchored_windows(citation, snippet):
            left, right = _window_bounds(snippet, start, len(citation))
            ratio = SequenceMatcher(None, snippet[left:right], citation).ratio()
            if ratio <= _SNAP_MIN_RATIO:
                continue
            delta = tuple(_refused_delta(_tokens(_margin(snippet, left, right)), citation_tokens))
            if delta and ratio > composed_ratio:
                composed, composed_ratio = delta, ratio
            elif not delta and ratio > best_ratio:
                best, best_ratio = snippet[left:right], ratio
    return CitationSnap(best, () if best is not None else composed)


def _tokens(text: str) -> list[str]:
    """*text* as casefolded words, so a re-punctuated clause has an empty delta."""
    return _SNAP_TOKEN_RE.findall(text.casefold())


def _refused_delta(window: Sequence[str], citation: Sequence[str]) -> Iterator[str]:
    """Name each token difference a re-extraction must not forgive; nothing for a paraphrase."""
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, window, citation, autojunk=False).get_opcodes():
        dropped, added = window[i1:i2], citation[j1:j2]
        if tag == "equal" or (_all_admissible(dropped) and _all_admissible(added)):
            continue
        if tag == "delete":
            # A delete at either end is the margin's own edge, not a word the model dropped.
            if i1 and i2 != len(window):
                yield f"a dropped {_render(dropped)}"
        elif tag == "insert":
            yield f"an added {_render(added)}"
        elif not _is_truncated_edge(window, citation, (i1, i2, j1, j2)):
            yield f"{_render(added)} where the snippet has {_render(dropped)}"


def _is_truncated_edge(window: Sequence[str], citation: Sequence[str], span: tuple[int, int, int, int]) -> bool:
    """True for the one-token stub a length-guessed window leaves at a citation's TAIL.

    The head is not carved out at all. A citation token that is the window token's SUFFIX is
    indistinguishable from morphological negation — `unsafe` -> `safe`, `disallowed` ->
    `allowed` — and a citation that genuinely begins mid-word is rare, so that edge fails
    closed rather than admit a row whose quote asserts the opposite of its own rule (#4716).
    """
    i1, _, j1, j2 = span
    if j2 - j1 != 1 or j2 != len(citation) or citation[j1] in _SNAP_NEGATORS:
        return False
    word, cut = window[i1], citation[j1]
    if word in _SNAP_NEGATORS or not word.startswith(cut):
        return False
    return not word[len(cut) :].startswith(_SNAP_NEGATING_SUFFIXES)


def _all_admissible(tokens: Sequence[str]) -> bool:
    return all(token in _SNAP_ADMISSIBLE_DELTA for token in tokens)


def _render(tokens: Sequence[str]) -> str:
    head = tokens[:_SNAP_RENDER_MAX_TOKENS]
    return repr(" ".join(head) + ("…" if len(tokens) > len(head) else ""))


def _margin(snippet: str, left: int, right: int) -> str:
    """The located window plus enough either side that its own ends are not read as a delta.

    The head moves forward to a word boundary so a half-word never surfaces in a refusal
    message as a word the snippet supposedly carries; a half-word at the tail is only ever
    a free tail delete.
    """
    head = max(0, left - _SNAP_WINDOW_MARGIN_CHARS)
    if head:
        boundary = snippet.find(" ", head)
        head = left if boundary == -1 else boundary + 1
    return snippet[head : right + _SNAP_WINDOW_MARGIN_CHARS]


def _window_bounds(snippet: str, start: int, length: int) -> tuple[int, int]:
    """The *length*-ish slice of *snippet* at *start*, widened out to whole words.

    A near-miss is shorter or longer than the text it quotes, so the anchor-derived offset
    lands a few characters inside a word. Recording a citation that begins mid-token is
    still verbatim but reads as damaged, so both ends move out to the nearest boundary.
    """
    left = snippet.rfind(" ", 0, start + 1) + 1 if start > 0 else 0
    right = snippet.find(" ", left + length)
    return left, len(snippet) if right == -1 else right


def _anchored_windows(citation: str, snippet: str) -> set[int]:
    """Candidate start offsets in *snippet* where *citation* may align."""
    starts: set[int] = set()
    for fraction in _SNAP_ANCHOR_OFFSETS:
        offset = int(len(citation) * fraction)
        anchor = citation[offset : offset + _SNAP_ANCHOR_CHARS]
        if len(anchor) < _SNAP_ANCHOR_CHARS:
            continue
        found = snippet.find(anchor)
        while found != -1:
            starts.add(max(0, found - offset))
            found = snippet.find(anchor, found + 1)
    return starts


__all__ = ["CitationSnap", "snap_citation"]
