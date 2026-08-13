"""Does this publish body reproduce the operator's own raw words? (#4195).

The banned-terms gate (#1415) answers *"does this text contain a forbidden
token?"*; the quote-scanner (#1213) answers *"does this text have the SHAPE of a
quotation?"*. Neither answers *"is this text someone's private message being
republished?"* — a body can be free of banned terms, carry no quote-shaped
heading, and still be a verbatim paste of the operator's chat. That is the gap
that let a public issue go out carrying the operator's own messages as
blockquotes after the one banned term in it was paraphrased away.

This module is pure detection over a per-session ledger of what the operator
actually said:

* :func:`record_operator_message` fingerprints an inbound operator message.
* :func:`scan_body` asks whether a candidate publish body reproduces one.

**The ledger never stores the operator's words.** It holds salted BLAKE2b
digests of overlapping :data:`SHINGLE_WORDS`-word runs and nothing else, so a
reader of the file cannot recover the message text — the same doctrine the
quote-blocklist states ("blocklists must not embed the raw quotes they protect
against"). The offending span the gate names comes from the CANDIDATE body,
which the agent already holds, never from the ledger.

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

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from teatree.hooks._hook_state import hook_state_root, note_env_override_once
from teatree.hooks._parser_primitives import is_fail_closed_sentinel as _is_fail_closed_sentinel
from teatree.hooks._publish_detection import segment_word_lists_raw
from teatree.hooks._quote_normalize import normalize_quotes
from teatree.hooks.publish_surface import is_gh_glab_posting_command

type Outcome = Literal["clean", "reproduced", "unknown"]

CLEAN: Final[Outcome] = "clean"
REPRODUCED: Final[Outcome] = "reproduced"
UNKNOWN: Final[Outcome] = "unknown"

OVERRIDE_ENV: Final[str] = "ALLOW_VERBATIM_PASTE"

# Words per fingerprinted run. Eight is short enough that a sentence lifted out
# of a paragraph still matches, and long enough that an ordinary turn of phrase
# shared by two independently-written texts does not.
SHINGLE_WORDS: Final[int] = 8

# Consecutive verbatim words that constitute reproduction, per window.
QUOTED_RUN_WORDS: Final[int] = 8
PROSE_RUN_WORDS: Final[int] = 40

LEDGER_VERSION: Final[int] = 1
MAX_RECORDED_MESSAGES: Final[int] = 25
MAX_FINGERPRINTS_PER_MESSAGE: Final[int] = 4000
_SPAN_CHARS: Final[int] = 200
_SALT_BYTES: Final[int] = 16


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


def _fingerprints(words: list[str], salt: str) -> list[str]:
    key = salt.encode("utf-8")[: hashlib.blake2b.MAX_KEY_SIZE]
    return [
        hashlib.blake2b("\x1f".join(words[i : i + SHINGLE_WORDS]).encode("utf-8"), key=key, digest_size=8).hexdigest()
        for i in range(len(words) - SHINGLE_WORDS + 1)
    ]


def _longest_run(words: list[str], known: frozenset[str], salt: str) -> tuple[int, int]:
    """Start index and word length of the longest run of ``words`` present in ``known``."""
    best_start = best_len = 0
    run_start: int | None = None
    for index, fingerprint in enumerate(_fingerprints(words, salt)):
        if fingerprint not in known:
            run_start = None
            continue
        if run_start is None:
            run_start = index
        length = index - run_start + SHINGLE_WORDS
        if length > best_len:
            best_start, best_len = run_start, length
    return best_start, best_len


def ledger_path(session_id: str, root: Path | None = None) -> Path:
    """Where this session's operator-message fingerprints live."""
    base = root if root is not None else hook_state_root()
    return base / f"operator-messages-{session_id}.json"


def _read_ledger(session_id: str, root: Path | None) -> tuple[str, list[list[str]]] | None:
    """The ledger's ``(salt, per-message fingerprints)``, or ``None`` when unreadable."""
    if not session_id:
        return None
    try:
        raw = json.loads(ledger_path(session_id, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != LEDGER_VERSION:
        return None
    salt = raw.get("salt")
    messages = raw.get("messages")
    if not isinstance(salt, str) or not salt or not isinstance(messages, list):
        return None
    return salt, [message for message in messages if isinstance(message, list)]


def record_operator_message(text: str, *, session_id: str, root: Path | None = None) -> bool:
    """Fingerprint one inbound operator message into this session's ledger.

    Returns whether the ledger was written. A message too short to shingle still
    writes (an empty fingerprint list): the ledger's EXISTENCE is what tells
    :func:`scan_body` the session is checkable, so a session of only short
    prompts answers :data:`CLEAN` — genuinely nothing long enough to reproduce —
    rather than degrading to :data:`UNKNOWN`.
    """
    if not session_id:
        return False
    target = ledger_path(session_id, root)
    existing = _read_ledger(session_id, root)
    salt, prior = existing if existing is not None else (secrets.token_hex(_SALT_BYTES), [])
    fresh = _fingerprints(_words(text), salt)[:MAX_FINGERPRINTS_PER_MESSAGE]
    payload = {
        "version": LEDGER_VERSION,
        "salt": salt,
        "messages": [*prior, fresh][-MAX_RECORDED_MESSAGES:],
    }
    tmp = target.with_suffix(".json.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(target)
    except OSError:
        return False
    return True


def scan_body(body: str, *, session_id: str, root: Path | None = None) -> Verdict:
    """Whether ``body`` reproduces a recorded operator message verbatim.

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
    ledger = _read_ledger(session_id, root)
    if ledger is None:
        reason = (
            "no session id on this call"
            if not session_id
            else "no operator-message history was recorded for this session"
        )
        return Verdict(UNKNOWN, reason=reason)
    salt, messages = ledger
    known = frozenset(fingerprint for message in messages for fingerprint in message)
    if not known:
        return Verdict(CLEAN)
    for source, threshold in ((_quoted_regions(body), QUOTED_RUN_WORDS), (body, PROSE_RUN_WORDS)):
        words = _words(source)
        start, length = _longest_run(words, known, salt)
        if length >= threshold:
            return Verdict(REPRODUCED, span=" ".join(words[start : start + length])[:_SPAN_CHARS], words=length)
    return Verdict(CLEAN)


def has_override(command: str) -> bool:
    """Whether the caller explicitly opted this publish out of the check.

    ``ALLOW_VERBATIM_PASTE=1`` is honoured as a leading inline env-assignment on
    the segment that IS the publish (so a decoy on an unrelated chained segment
    cannot vouch for it, mirroring #2031), and from the process environment,
    where a standing export is announced once per session rather than silently
    disabling every scan.
    """
    for words in segment_word_lists_raw(command):
        if _segment_leads_with_override(words) and is_gh_glab_posting_command(" ".join(words)):
            return True
    if os.environ.get(OVERRIDE_ENV, "").strip() == "1":
        note_env_override_once(OVERRIDE_ENV)
        return True
    return False


def _segment_leads_with_override(words: list[str]) -> bool:
    for word in words:
        name, sep, value = word.partition("=")
        if not sep:
            return False
        if name == OVERRIDE_ENV:
            return value.strip() == "1"
    return False


def format_block_message(verdict: Verdict) -> str:
    """The PreToolUse deny reason for a reproduced operator message."""
    return (
        "BLOCKED: verbatim operator-paste gate (#4195). This body reproduces "
        f"{verdict.words} consecutive words of a message the operator sent in this session: "
        f'"{verdict.span}". Their private words are not yours to publish — paraphrase the span '
        "into your own author-voice summary. This is independent of the banned-terms list, so "
        "substituting a word will not clear it. If the quote is deliberate and sanctioned, "
        f"re-issue the command with a leading {OVERRIDE_ENV}=1 env prefix (it is recorded)."
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
