"""Fail-closed evidence gate for outcome-claiming task completions (#1280).

The recurrence this forecloses: a task was repeatedly marked completed with
a free-text note that ASSERTED an external outcome — "merged via X",
"posted the review", "shipped to prod", "deployed to staging" — but carried NO
resolvable pointer to the artifact that would let anyone confirm the claim.
The phantom-completion then surfaces later as a "done-but-not-done" bug.

A remembered rule ("done claims require artifact evidence") did not hold under
load; this module is the deterministic substitute, mirroring the
:mod:`teatree.core.gates.dod_gate` shape: a pure function over the completion note,
a dedicated error subclass, and a clear message naming exactly what is missing.

Scope is deliberately narrow — it is NON-breaking by construction. Two separate
judgments, NOT one:

Outcome assertion (the trigger)
    A note asserts an external outcome when an outcome verb (one of
    :data:`OUTCOME_CLAIM_KINDS`, plus surface synonyms in
    :data:`_CLAIM_SYNONYMS`) either CO-OCCURS with an artifact/context cue — a
    branch / MR / PR / issue / commit word, a deploy target (``to prod`` /
    ``to staging`` / …), a review surface, a ``via`` claim connector, an
    artifact-path-shaped token, or an already-resolvable pointer — OR OPENS the
    note as a bare verb (the canonical terse phantom shape "merged" / "it's
    merged" / "merged to main", with no further cue). A note that merely
    CONTAINS an outcome verb mid-prose while describing internal code work
    ("merged the two helper functions", "released the lock", "merge conflict
    resolved") does NOT assert an outcome and is never gated.

Resolvable pointer (the evidence)
    Once a note asserts an outcome, it MUST also contain something an auditor
    could actually follow: a full URL, a git SHA carrying a commit cue
    (``commit``/``sha``/``rev``/``@`` adjacent to the hex run), an MR/PR/issue
    reference (``!123`` / ``#123``), a forge note id (``note_xxx``), or a
    real-looking file/module path (filesystem-rooted, carrying a known file
    extension, or a dotted module path anchored on a known top-level package).
    A bare two-word ``a/b``, a bare hex/digit run with no commit cue (an
    all-digit build number or a dictionary-word hex run like ``deadbeef``), and
    ordinary dotted prose (``the.thing.now``) do NOT count — they signal a
    claim (so the note is an assertion) without backing it (so the gate
    refuses), which is exactly the spoof the gate must catch.

Ordinary completion
    A completion with NO note, or a note that records internal progress with
    no asserted outcome, is untouched. This is the common ``tasks complete`` /
    internal FSM path and must keep working.

The gate is invoked from the single out-of-band completion surface
(``tasks complete --note``) — the place an agent records "this work landed
externally". On a block it raises :class:`CompletionEvidenceError`; the task
is NOT marked completed.
"""

import re
from dataclasses import dataclass

from teatree.core.models.errors import InvalidTransitionError

# The external-outcome claim kinds that REQUIRE a resolvable artifact pointer.
# Matched against the note's verbs (see ``_CLAIM_SYNONYMS``); a note with none
# of these can never assert an external outcome.
OUTCOME_CLAIM_KINDS = frozenset({"merged", "posted", "shipped", "deployed"})

# Surface synonyms that map onto an outcome claim kind. "landed" and "released"
# are the spellings the agent actually uses in completion notes for the same
# external facts the canonical kinds name.
_CLAIM_SYNONYMS: dict[str, str] = {
    "merged": "merged",
    "merge": "merged",
    "landed": "merged",
    "posted": "posted",
    "post": "posted",
    "published": "posted",
    "shipped": "shipped",
    "ship": "shipped",
    "released": "shipped",
    "release": "shipped",
    "deployed": "deployed",
    "deploy": "deployed",
}

# Longest-first so a verb that is a prefix of another (``merge`` vs ``merged``)
# does not preempt the more specific spelling.
_CLAIM_VERBS: list[str] = sorted(_CLAIM_SYNONYMS, key=lambda verb: -len(verb))
_CLAIM_VERB_RE = re.compile(r"\b(" + "|".join(_CLAIM_VERBS) + r")\b", re.IGNORECASE)

# A NOTE-INITIAL bare outcome verb is the canonical phantom shape: the verb is
# essentially the WHOLE note — optionally after a leading ``it's`` / ``its`` /
# ``it is``, and optionally trailed by a bare ``to <destination>`` — with no
# object noun phrase reframing it as internal work ("merged", "it's merged",
# "merged to main"). Such a note asserts an outcome on its own, with no further
# context cue, so the terse phantom completion is gated rather than slipping
# through ungated. A verb that LEADS a longer sentence ("shipped the feature",
# "merged the two helper functions") carries an object and is NOT this shape.
_NOTE_INITIAL_VERB_RE = re.compile(
    r"^\s*(?:it(?:'s|s|\s+is)\s+)?(?:" + "|".join(_CLAIM_VERBS) + r")"
    r"(?:\s+to\s+\w+)?\s*[.!]*\s*$",
    re.IGNORECASE,
)

# Phrases where an outcome verb is part of an INTERNAL-work idiom, not an
# external-outcome claim. Stripped before assertion detection so the verb in
# "merge conflict" / "not to merge yet" never trips the trigger.
_INTERNAL_IDIOM_RE = re.compile(
    r"\b(?:merge\s+conflict|not\s+to\s+(?:merge|ship|post|deploy|release)|"
    r"(?:merge|ship|post|deploy|release)\s+(?:yet|later))\b",
    re.IGNORECASE,
)

# Context cues that turn an outcome verb into a CLAIM about an external
# artifact. Deliberately broader than the pointer-validity check: a token may
# signal "this is a claim" (so the note asserts an outcome and is gated)
# without itself being a valid pointer (so the gate then refuses for lack of
# evidence) — e.g. ``merged a/b`` and ``merged the deadbeef branch``.
_CLAIM_CONTEXT_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:branch|mr|pr|merge\s+request|pull\s+request|issue|commit|tag|release)\b", re.IGNORECASE),
    re.compile(r"\breview\b", re.IGNORECASE),
    re.compile(r"\bto\s+(?:prod|production|staging|dev|qa|uat|preprod|live)\b", re.IGNORECASE),
    re.compile(r"\bvia\b", re.IGNORECASE),
    re.compile(r"[!#]\d+"),  # an MR/PR/issue reference is itself a claim cue
    re.compile(r"\bnote_[A-Za-z0-9]+\b"),  # a forge note id is a claim cue
    re.compile(r"https?://\S+"),  # a URL is a claim cue
    re.compile(r"\b\w[\w.-]*/[\w./-]+"),  # an artifact-path-shaped token (a/b, src/x)
)

# --- Resolvable-pointer (evidence) patterns -------------------------------

_URL_RE = re.compile(r"https?://\S+")
_ISSUE_REF_RE = re.compile(r"[!#]\d+")
_NOTE_ID_RE = re.compile(r"\bnote_[A-Za-z0-9]+\b")

# A SHA counts only when a commit cue sits adjacent (``commit``/``sha``/``rev``
# or a leading ``@``). A bare hex/digit run is NOT a pointer: an all-digit
# build/ticket number (``build 1234567890``) or an English hex word
# (``deadbeef``) reads as a claim, not as evidence the claim can be followed.
_CUED_SHA_RE = re.compile(
    r"(?:\b(?:commit|sha|rev|revision)\b\s+|@)([0-9a-f]{7,40})\b",
    re.IGNORECASE,
)

# A known source/config file extension makes a token a real file path.
_FILE_EXT_RE = re.compile(
    r"\.(?:py|md|rst|txt|toml|cfg|ini|ya?ml|json|js|ts|tsx|jsx|html|css|scss|"
    r"sh|sql|go|rs|java|kt|rb|c|h|cpp|hpp|xml|lock|env)\b",
    re.IGNORECASE,
)
# A filesystem-rooted path (``/...``, ``./...``, ``src/...``, ``tests/...``).
_ROOTED_PATH_RE = re.compile(
    r"(?:^|\s)(?:\.{0,2}/|(?:src|tests?|docs?|scripts?|e2e|lib|app|pkg|cmd|internal)/)[\w./-]+",
    re.IGNORECASE,
)
# A dotted module path. A bare ≥3-segment lowercase dotted token is too loose —
# ordinary prose (``fix the.thing.now``) matches it — so a real module path must
# either be anchored on a recognised top-level package/module segment
# (``teatree.`` / ``src.`` / ``tests.`` / ``docs.``) or end in a known source
# file extension (``foo.bar.baz.py``).
_DOTTED_MODULE_RE = re.compile(
    r"(?<![\w.])(?:(?:teatree|src|tests?|docs?)(?:\.[a-z_]\w*)+"
    r"|[a-z_]\w*(?:\.[a-z_]\w*)+\.(?:py|pyi))\b",
    re.IGNORECASE,
)

# A Slack message timestamp is ``<epoch-seconds>.<microseconds>`` — ten digits,
# a dot, six digits (Slack's ``ts`` format). An answerer records its post as a
# Slack ts; the channel-bearing forms (``slack:<channel>:<ts>`` and bare
# ``<channel>:<ts>``) carry a Slack channel id (``C``/``D``/``G`` + uppercase
# alnum), the bare ``<ts>`` does not. All three are auditor-followable pointers.
_SLACK_TS = r"\d{10}\.\d{6}"
_SLACK_CHANNEL = r"[CDG][A-Z0-9]{6,}"
_SLACK_QUALIFIED_TS_RE = re.compile(rf"\b(?:slack:)?(?P<channel>{_SLACK_CHANNEL}):(?P<ts>{_SLACK_TS})\b")
_SLACK_BARE_TS_RE = re.compile(rf"(?<![\d.]){_SLACK_TS}(?![\d.])")


class CompletionEvidenceError(InvalidTransitionError):
    """A completion asserting an external outcome carried no resolvable pointer.

    A subclass of :class:`InvalidTransitionError` (sibling of
    ``DirtyWorktreeError`` / ``DodLocalE2EError``) so a caller inside the
    Task completion transaction sees the same refusal class and the FSM stays
    put. The message names the missing piece (the claim kind plus the kinds of
    pointer that would satisfy it) so the agent can supply it without guessing.
    """


@dataclass(frozen=True)
class CompletionEvidence:
    """Evidence backing an outcome-claiming completion.

    ``claim_kind`` is one of :data:`OUTCOME_CLAIM_KINDS` (or empty when no
    outcome is ASSERTED); ``artifact_pointer`` is the note text the pointer is
    resolved from; ``fresh_observation`` is an optional free-text note of what
    was observed when the claim was made.
    """

    claim_kind: str
    artifact_pointer: str
    fresh_observation: str = ""

    @property
    def asserts_outcome(self) -> bool:
        return self.claim_kind in OUTCOME_CLAIM_KINDS

    @property
    def has_resolvable_pointer(self) -> bool:
        return has_resolvable_pointer(self.artifact_pointer)


def detect_claim_kind(note: str) -> str:
    """Return the outcome verb's canonical kind, or ``""`` when none is present.

    Pure verb detection — it answers "does an outcome word appear", not "does
    the note assert an outcome". The FIRST matching verb wins, mapped through
    ``_CLAIM_SYNONYMS``. Use :func:`asserts_outcome` for the gate trigger.
    """
    match = _CLAIM_VERB_RE.search(note or "")
    if match is None:
        return ""
    return _CLAIM_SYNONYMS[match.group(1).lower()]


def asserts_outcome(note: str) -> bool:
    """True iff *note* CLAIMS an external outcome actually happened.

    Two paths assert an outcome. (1) An outcome verb co-occurs with an
    artifact/context cue (a branch/MR/PR/issue/commit word, a deploy target, a
    review surface, a ``via`` connector, a pointer token, or an
    artifact-path-shaped token). (2) The note opens with a bare outcome verb
    (the canonical terse phantom shape — "merged", "it's merged",
    "merged to main"), which asserts on its own with no further cue.
    Internal-work idioms ("merge conflict", "not to merge yet") are stripped
    first, so a note describing code work that merely contains an outcome verb
    is NOT an assertion.
    """
    text = _INTERNAL_IDIOM_RE.sub(" ", note or "")
    if _CLAIM_VERB_RE.search(text) is None:
        return False
    if _NOTE_INITIAL_VERB_RE.search(text):
        return True
    return any(pattern.search(text) for pattern in _CLAIM_CONTEXT_RES)


def has_resolvable_pointer(note: str) -> bool:
    """True iff *note* contains at least one auditor-followable pointer.

    A full URL, an MR/PR/issue reference, a forge note id, a cued git SHA (a
    hex run with a ``commit``/``sha``/``rev``/``@`` cue), a real-looking path
    (rooted, extensioned, or an anchored dotted module path), or a Slack message
    ts (``slack:<channel>:<ts>`` / ``<channel>:<ts>`` / bare ``<ts>``). A bare
    ``a/b`` token, a bare hex/digit run with no commit cue, and ordinary dotted
    prose are intentionally NOT pointers.
    """
    text = note or ""
    return any(
        pattern.search(text)
        for pattern in (
            _URL_RE,
            _ISSUE_REF_RE,
            _NOTE_ID_RE,
            _CUED_SHA_RE,
            _FILE_EXT_RE,
            _ROOTED_PATH_RE,
            _DOTTED_MODULE_RE,
            _SLACK_QUALIFIED_TS_RE,
            _SLACK_BARE_TS_RE,
        )
    )


def normalize_artifact_pointers(note: str) -> str:
    """Rewrite channel-bearing Slack-ts pointers to the archives permalink.

    ``slack:<channel>:<ts>`` and bare ``<channel>:<ts>`` become
    ``https://slack.com/archives/<channel>/p<ts-without-dot>`` — the canonical,
    auditor-followable form stored on the completion. A bare ``<ts>`` (no
    channel) cannot form a permalink and is left as-is; everything else (real
    URLs, SHAs, paths) is untouched. This is the single normalization seam:
    the ``tasks complete`` note is canonicalized UP through it before both the
    evidence gate and storage.
    """

    def _to_permalink(match: re.Match[str]) -> str:
        channel = match.group("channel")
        ts = match.group("ts").replace(".", "")
        return f"https://slack.com/archives/{channel}/p{ts}"

    return _SLACK_QUALIFIED_TS_RE.sub(_to_permalink, note or "")


def evidence_from_note(note: str) -> CompletionEvidence:
    """Parse a free-text completion note into a :class:`CompletionEvidence`.

    The note itself IS the evidence carrier on the ``tasks complete --note``
    surface. ``claim_kind`` is set only when the note ASSERTS an outcome (verb
    plus context cue); a note that merely mentions an outcome verb while
    describing internal work yields an empty, non-asserting evidence value.
    """
    claim_kind = detect_claim_kind(note) if asserts_outcome(note) else ""
    return CompletionEvidence(claim_kind=claim_kind, artifact_pointer=note or "")


def check_completion_evidence(note: str) -> None:
    """Refuse an outcome-claiming completion that carries no resolvable pointer.

    Fail-closed: a note that ASSERTS an external outcome (merged / posted /
    shipped / deployed, verb plus context cue) MUST contain a resolvable
    pointer. A note that asserts nothing external — including internal-progress
    notes that merely contain an outcome verb, and the no-note path — passes
    untouched, so ordinary internal completions are never gated.
    """
    evidence = evidence_from_note(note)
    if not evidence.asserts_outcome:
        return
    if evidence.has_resolvable_pointer:
        return
    msg = (
        f"Refusing to complete: the note asserts an external outcome "
        f"({evidence.claim_kind!r}) but carries no resolvable artifact pointer. "
        f"Add a pointer the claim can be verified against — a URL, a git SHA, "
        f"an MR/PR/issue reference (!123 / #123), a forge note id, or a file "
        f"path — e.g. `--note '{evidence.claim_kind} via <url-or-!id-or-sha>'`."
    )
    raise CompletionEvidenceError(msg)
