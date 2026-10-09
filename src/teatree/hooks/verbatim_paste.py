"""Does this publish body reproduce the operator's own raw words? (#4195).

The banned-terms gate (#1415) answers *"does this text contain a forbidden
token?"*; the quote-scanner (#1213) answers *"does this text have the SHAPE of a
quotation?"*. Neither answers *"is this text someone's private message being
republished?"* — a body can be free of banned terms, carry no quote-shaped
heading, and still be a verbatim paste of the operator's chat. That is the gap
that let a public issue go out carrying the operator's own messages as
blockquotes after the one banned term in it was paraphrased away.

This module is pure detection: :func:`scan_body` asks whether a candidate
publish body reproduces one of the operator's messages, which the caller reads
from the session transcript. Nothing about those messages is persisted. The
offending span the gate names comes from the CANDIDATE body, which the agent
already holds.

Two windows, because verbatim paste concentrates in quotations but is not
confined to them: a run inside a quoted region (a blockquote line, a long
double-quoted span) refuses at :data:`QUOTED_RUN_WORDS`, while running prose
must reach :data:`PROSE_RUN_WORDS` before it counts — long enough that a
restated ticket title or a shared technical phrase cannot trip it.

Failure posture is asymmetric by design (#4041): a REFUSAL is fail-closed, but
an inability to read the history, or a candidate body the caller could not
resolve, reports :data:`UNKNOWN` rather than :data:`CLEAN`, so "I could not
check" can never be mistaken for "I checked and it was fine".
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from teatree.hooks._hook_state import hook_state_root
from teatree.hooks._parser_primitives import is_fail_closed_sentinel as _is_fail_closed_sentinel
from teatree.hooks._quote_normalize import normalize_quotes

type Outcome = Literal["clean", "reproduced", "unknown"]

CLEAN: Final[Outcome] = "clean"
REPRODUCED: Final[Outcome] = "reproduced"
UNKNOWN: Final[Outcome] = "unknown"

# Words per fingerprinted run. Eight is short enough that a sentence lifted out
# of a paragraph still matches, and long enough that an ordinary turn of phrase
# shared by two independently-written texts does not.
SHINGLE_WORDS: Final[int] = 8

# Consecutive verbatim words that constitute reproduction, per window.
QUOTED_RUN_WORDS: Final[int] = 8
PROSE_RUN_WORDS: Final[int] = 40

MAX_RECORDED_MESSAGES: Final[int] = 25
MAX_FINGERPRINTS_PER_MESSAGE: Final[int] = 4000
_SPAN_CHARS: Final[int] = 200


@dataclass(frozen=True)
class Verdict:
    """One :func:`scan_body` answer.

    ``span`` carries the reproduced run (normalised words from the candidate
    body) on :data:`REPRODUCED`; ``reason`` carries why the check could not run
    on :data:`UNKNOWN`.
    """

    outcome: Outcome
    span: str = ""
    words: int = 0
    reason: str = ""


# A fenced block is a technical artifact the agent is expected to reproduce
# verbatim (a command, a log, a diff), not the operator's voice — excluded from
# both the recorded message and the scanned body so reproducing one is never a
# finding. Inline code and URLs are excluded for the same reason.
#
# The close marker is REQUIRED (no ``\Z`` end-of-string fallback, #4195
# review Blocker 3): the fallback let one stray, unterminated opening fence
# excise everything from that point to the end of the body, so a single
# ``` line anywhere made the entire remainder invisible to the scan. An
# unterminated fence is left as ordinary text instead — undercounting a
# malformed fence as prose is the safe direction; over-excising is not.
_FENCE_RE: Final[re.Pattern[str]] = re.compile(r"^[ \t]*(?:```|~~~).*?^[ \t]*(?:```|~~~)", re.MULTILINE | re.DOTALL)
_INLINE_CODE_RE: Final[re.Pattern[str]] = re.compile(r"`[^`\n]*`")
_URL_RE: Final[re.Pattern[str]] = re.compile(r"\b\w+://\S+")
_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-z]+(?:'[a-z]+)*")

_BLOCKQUOTE_RE: Final[re.Pattern[str]] = re.compile(r"^[ \t]*>+[ \t]?(.*)$", re.MULTILINE)
# 20 chars is the length the quote-scanner already uses to tell a real
# quotation from an incidentally-quoted word.
_QUOTED_SPAN_RE: Final[re.Pattern[str]] = re.compile(r'"([^"\n]{20,})"')


def _words(text: str) -> list[str]:
    """Lower-cased word tokens, with code and URLs excised.

    Quotes are normalised FIRST so a contraction tokenises identically
    regardless of which apostrophe glyph it carries: :func:`_quoted_regions`
    already normalises before extracting a quoted span, and the recorder and
    the plain-prose window must shingle a smart-quoted operator message the
    same way or the two windows silently diverge (#4195 review).
    """
    stripped = _URL_RE.sub(" ", _INLINE_CODE_RE.sub(" ", _FENCE_RE.sub(" ", normalize_quotes(text))))
    return _WORD_RE.findall(stripped.lower())


def _quoted_regions(text: str) -> str:
    """The blockquote lines and long double-quoted spans of ``text``, joined."""
    normalized = normalize_quotes(text)
    parts = [*_BLOCKQUOTE_RE.findall(normalized), *_QUOTED_SPAN_RE.findall(normalized)]
    return "\n".join(parts)


type Shingle = tuple[str, ...]


def _shingles(words: list[str]) -> list[Shingle]:
    return [tuple(words[i : i + SHINGLE_WORDS]) for i in range(len(words) - SHINGLE_WORDS + 1)]


def _longest_run(words: list[str], known: frozenset[Shingle]) -> tuple[int, int]:
    """Start index and word length of the longest run of ``words`` present in ``known``."""
    best_start = best_len = 0
    run_start: int | None = None
    for index, shingle in enumerate(_shingles(words)):
        if shingle not in known:
            run_start = None
            continue
        if run_start is None:
            run_start = index
        length = index - run_start + SHINGLE_WORDS
        if length > best_len:
            best_start, best_len = run_start, length
    return best_start, best_len


def scan_body(body: str, *, operator_messages: Sequence[str] | None) -> Verdict:
    """Whether ``body`` reproduces one of ``operator_messages`` verbatim.

    ``None`` means the session's history could not be read, which is
    :data:`UNKNOWN`; an empty sequence is a session with nothing to reproduce.

    The quoted window is tested first — it is where verbatim paste concentrates
    and where the shorter :data:`QUOTED_RUN_WORDS` threshold applies — then the
    whole body at :data:`PROSE_RUN_WORDS`.

    A ``body`` the command parser could not resolve (a missing ``--body-file``,
    an unexpanded ``$VAR``, a stdin body) carries an injected fail-closed
    sentinel rather than real content — there is nothing to shingle, so this is
    :data:`UNKNOWN`, never a scan that happened to find nothing (#4195 review;
    the sibling quote-scanner and banned-terms gates recognise the same
    sentinel before content matching, for the same reason).
    """
    if _is_fail_closed_sentinel(body):
        return Verdict(UNKNOWN, reason="the publish body could not be resolved before the command runs")
    if operator_messages is None:
        return Verdict(UNKNOWN, reason="this session's transcript could not be read")
    known = frozenset(
        shingle
        for message in operator_messages
        for shingle in _shingles(_words(message))[:MAX_FINGERPRINTS_PER_MESSAGE]
    )
    if not known:
        return Verdict(CLEAN)
    for source, threshold in ((_quoted_regions(body), QUOTED_RUN_WORDS), (body, PROSE_RUN_WORDS)):
        words = _words(source)
        start, length = _longest_run(words, known)
        if length >= threshold:
            return Verdict(REPRODUCED, span=" ".join(words[start : start + length])[:_SPAN_CHARS], words=length)
    return Verdict(CLEAN)


def format_block_message(verdict: Verdict) -> str:
    """The PreToolUse deny reason for a reproduced operator message."""
    return (
        "BLOCKED: verbatim operator-paste gate (#4195). This body reproduces "
        f"{verdict.words} consecutive words of a message the operator sent in this session: "
        f'"{verdict.span}". Their private words are not yours to publish — paraphrase the span '
        "into your own author-voice summary. This is independent of the banned-terms list, so "
        "substituting a word will not clear it. Rephrase without the quoted span or ask the owner. "
        "Ask the owner to review the wording before publishing."
    )


def format_unknown_message(verdict: Verdict) -> str:
    """The stderr NOTE for a check that could not run — never reported as clean."""
    return (
        f"NOTE: verbatim operator-paste gate (#4195) could NOT check this body — {verdict.reason}. "
        "This is UNKNOWN, not a clean scan: verify by hand that the body does not reproduce the "
        "operator's own messages before it reaches a public surface.\n"
    )


def log_decision(*, decision: str, verdict: Verdict, ledger: Path | None = None) -> None:
    """Append one audit record — the span itself is deliberately NOT written."""
    record = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "decision": decision,
        "outcome": verdict.outcome,
        "words": verdict.words,
        "reason": verdict.reason,
    }
    target = ledger if ledger is not None else hook_state_root() / "verbatim-paste.jsonl"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        return
