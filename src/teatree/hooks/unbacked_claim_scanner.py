r"""Unbacked-claim detector — a diagnosis or an alarm cites what was read.

The completion-claim gate (#2665) demands per-deliverable evidence for a DONE
claim. The same principle was missing one step earlier, at the DIAGNOSTIC claim,
and two recorded failures show why.

A cause was asserted for a red pipeline — "it failed because 466 files were
meeting the gates for the first time" — generated from the check NAMES, with the
log never opened; the truth in the log was smaller and sharper. And a finding was
escalated at full severity while the evidence that would settle it, which the
agent's own brief had asked for, had not come back.

Both are a claim about a system's state with nothing read behind it, so one
requirement covers both: cite an artefact you actually read. The bar is
deliberately low — a fenced block, an inline span, a path, a ``file:line``, a
rule or exception code, a URL, a quoted line — because the cost of the gate must
be one citation, never a rewrite. What it refuses is the well-formed paragraph
with no artefact anywhere in the turn.

Two honest outs are not blocked, because they are the behaviour being asked for:
a diagnosis explicitly marked unverified ("I have not read the logs; my guess is
…") is a hypothesis, not a diagnosis; and downgrading an alarm you cannot yet
back is exactly right. A severity label is NOT clearable by hedging, though —
raising the alarm IS the claim — and an uncited one is named twice over when the
turn itself says the settling evidence is outstanding.

Fail-safe-to-silent everywhere: empty or odd input yields ``None``.
"""

import re
from typing import Final, NamedTuple

_FENCED_CODE_RE: Final[re.Pattern[str]] = re.compile(r"```.*?```", re.DOTALL)
_SENTENCE_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"[.!?\n]+")

# "blocked" is deliberately absent, from this list and from _CAUSAL_RE alike: it
# states a DEPENDENCY, not a failure, so "blocked on a human approval" and
# "blocked because the reviewer is away" are ordinary status lines, and reading
# either as an uncited diagnosis fired the gate on every honest one. Keeping the
# word here while dropping "blocked on" below left the exclusion vacuous — the
# generic "because" re-admitted the same false positive.
_FAILURE_WORD_RE: Final[re.Pattern[str]] = re.compile(
    r"\bfail(?:ed|s|ing|ures?)?\b|\bred\b|\bbroke\b|\bbroken\b|\breject(?:ed|s)?\b|"
    r"\berror(?:ed|s)?\b|\bcrash(?:ed|es)?\b|\btimed out\b|\brefused\b|"
    r"\bdid ?n[o']?t pass\b|\bwent red\b|\bregressed\b|\bregression\b",
    re.IGNORECASE,
)

# "blocked on X" is deliberately absent for the same reason. "failed on <step>"
# stays — that one does name a cause.
_CAUSAL_RE: Final[re.Pattern[str]] = re.compile(
    r"\bbecause\b|\bdue to\b|\bcaused by\b|\bthe cause\b|\broot cause\b|\bthe reason\b|"
    r"\bowing to\b|\bon account of\b|\bfailed on\b|\bthanks to\b",
    re.IGNORECASE,
)

# An escalation LABEL, not a word in prose: all-caps and either delimited or
# standing alone. Case-sensitive so "the critical path" never fires, and the scan
# runs on fence-stripped text so a quoted "CRITICAL:" log line does not either.
_SEVERITY_LABEL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[\s(\[>*_-])(?:\*\*|__)?(?:SEVERE|CRITICAL|BLOCKER|SHOWSTOPPER|URGENT|SEV-?[012]|P0)"
    r"(?:\*\*|__)?\s*[:!]"
    r"|(?:^|\s)(?:\*\*|__)?(?:SEVERE|BLOCKER|SHOWSTOPPER)(?:\*\*|__)?\b"
    r"|\U0001f6a8",
    re.MULTILINE,
)

# The turn says the evidence that would settle the claim is not in yet. Proximity
# in both directions, bounded to one clause, so an unrelated "awaiting CI" far
# from any evidence word does not count.
_UNSETTLED = (
    r"(?:awaiting|await|pending|outstanding|not yet|yet to|still (?:running|waiting|pending)|"
    r"haven'?t|have not|hasn'?t|has not|will confirm|before (?:i|we) can confirm|waiting on|"
    r"once it (?:returns|reports|comes back|lands))"
)
_EVIDENCE_WORD = (
    r"(?:evidence|confirmation|confirm|verif\w+|report|artefact|artifact|logs?|proof|"
    r"results?|ordering|output|readout)"
)
_EVIDENCE_OUTSTANDING_RE: Final[re.Pattern[str]] = re.compile(
    rf"{_UNSETTLED}[^.\n]{{0,80}}{_EVIDENCE_WORD}|{_EVIDENCE_WORD}[^.\n]{{0,80}}{_UNSETTLED}",
    re.IGNORECASE,
)

# An explicit "I have not checked this" — the honest alternative to a confident
# invention, so it must never be blocked.
_HEDGE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:haven'?t|have not|hasn'?t|has not) (?:yet )?(?:read|opened|checked|looked|seen|inspected)\b|"
    r"\bunverified\b|\bunconfirmed\b|\bnot (?:yet )?verified\b|\bhypothes\w+\b|\bspeculat\w+\b|"
    r"\b(?:my|a|an educated|best) guess\b|\b(?:i'?m|i am) guessing\b|"
    r"\bwithout (?:reading|opening|checking)\b|\bfrom the (?:check )?names? alone\b",
    re.IGNORECASE,
)

_FENCED_BLOCK_RE: Final[re.Pattern[str]] = re.compile(r"```[^\n]*\n(?P<body>.*?)```", re.DOTALL)
_INLINE_CODE_RE: Final[re.Pattern[str]] = re.compile(r"`([^`\n]{2,})`")
_FILE_LINE_RE: Final[re.Pattern[str]] = re.compile(r"\b[\w./-]+\.[A-Za-z]{1,5}:\d+\b")
_PATH_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[\w./-]*[\w-]\.(?:py|pyi|tsx?|jsx?|jsonl|json|ya?ml|toml|md|sh|sql|cfg|conf|ini|env|"
    r"txt|log|csv|lock|html|css)\b",
)
# A lint/rule code (`F401`, `PLR0911`) or a CamelCase exception name — one letter
# is enough, since the common ruff codes carry exactly one.
_CODE_IDENT_RE: Final[re.Pattern[str]] = re.compile(r"\b[A-Z]{1,5}\d{3,5}\b|\b\w+(?:Error|Exception|Warning)\b")
_URL_RE: Final[re.Pattern[str]] = re.compile(r"https?://\S+")
_QUOTED_LINE_RE: Final[re.Pattern[str]] = re.compile(r"^[ \t]*>\s*\S.{11,}", re.MULTILINE)


class ClaimVerdict(NamedTuple):
    """Why a claim was blocked: which trigger fired, its text, and what is missing."""

    kind: str
    claim: str
    missing: list[str]


def _has_evidence_citation(text: str) -> bool:
    """True when the turn quotes, names, or links something the agent read."""
    fenced = _FENCED_BLOCK_RE.search(text)
    if fenced is not None and fenced.group("body").strip():
        return True
    return any(
        pattern.search(text)
        for pattern in (_INLINE_CODE_RE, _FILE_LINE_RE, _PATH_TOKEN_RE, _CODE_IDENT_RE, _URL_RE, _QUOTED_LINE_RE)
    )


def _causal_failure_sentence(prose: str) -> str | None:
    """The first sentence asserting a cause for a failure, else ``None``."""
    for raw in _SENTENCE_SPLIT_RE.split(prose):
        sentence = raw.strip()
        if sentence and _FAILURE_WORD_RE.search(sentence) and _CAUSAL_RE.search(sentence):
            return sentence
    return None


def _severity_verdict(prose: str, text: str) -> ClaimVerdict | None:
    """An escalation with nothing read behind it, else ``None``.

    A citation clears the leg outright. The outstanding-evidence phrasing is an
    ADDITIONAL reason on an already-uncited alarm, never a reason on its own: an
    escalation that quotes its artefact AND says in the same breath that a
    further check is still running is an honest, backed report, and blocking it
    taxed exactly the disclosure the gate wants to encourage.
    """
    label = _SEVERITY_LABEL_RE.search(prose)
    if label is None or _has_evidence_citation(text):
        return None
    missing = ["the escalation cites no artefact — no quoted output, file:line, path, code, or link"]
    if _EVIDENCE_OUTSTANDING_RE.search(prose):
        missing.append("the settling evidence is still outstanding — the turn says it has not come back yet")
    return ClaimVerdict(kind="severity", claim=label.group(0).strip(), missing=missing)


def find_unbacked_claim(text: str) -> ClaimVerdict | None:
    """Return a verdict to BLOCK, or ``None`` to allow.

    Severity is judged first and is the stricter leg: an escalation label needs a
    citation, and hedging does not clear it the way it clears a diagnosis. A
    causal failure diagnosis needs only a citation, and an explicitly unverified
    one is a hypothesis rather than a diagnosis, so it never fires.
    """
    if not text:
        return None
    prose = _FENCED_CODE_RE.sub(" ", text)
    severity = _severity_verdict(prose, text)
    if severity is not None:
        return severity
    if _HEDGE_RE.search(prose) or _has_evidence_citation(text):
        return None
    sentence = _causal_failure_sentence(prose)
    if sentence is None:
        return None
    return ClaimVerdict(
        kind="diagnosis",
        claim=sentence,
        missing=["the diagnosis cites no artefact — no quoted output, file:line, path, code, or link"],
    )


def format_block_message(verdict: ClaimVerdict) -> str:
    """Render the BLOCK reason naming the claim and what would back it."""
    reasons = "\n".join(f"  - {reason}" for reason in verdict.missing)
    head = "a diagnosis" if verdict.kind == "diagnosis" else "an escalation"
    return (
        f"EVIDENCE GATE — this turn states {head} about a system's state with nothing "
        f'read behind it.\n  claim: "{verdict.claim}"\n{reasons}\n'
        "A plausible story assembled from check names is not a diagnosis. Open the log, "
        "the job, or the file, and quote the line that settles it — one fenced excerpt, "
        "a file:line, a rule or exception code, or a link is enough. If you have not read "
        "it yet, say exactly that instead ('I have not opened the logs; my guess is …'). "
        "An alarm waits for the evidence you asked for: downgrade it until that evidence "
        "is in hand. Escape for a genuine false fire: end the turn with "
        "[skip-evidence-gate: <reason>]."
    )
