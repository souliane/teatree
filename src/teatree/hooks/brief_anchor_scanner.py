r"""Dispatch-brief anchor detector — a sub-agent may overrule the brief that sent it.

Orchestrator-written briefs routinely assert specifics — a ``file:line``, a config
key, a count, whether an artifact exists — that are stale or simply wrong. Across
two recorded sessions that produced ~23 disconfirmed premises; several would have
rebuilt work that already existed, and one was published to a customer-facing page
as a blocker and was wrong when written (#4341).

The countermeasure that caught every one of them is a single sentence in the brief:
:data:`TRUST_THE_CODE_CLAUSE`. It works precisely because it needs no knowledge of
WHICH assertion is stale — and neither does this detector. It fires only when a
brief BOTH asserts verifiable specifics AND anchors none of them: no SHA anchor
("verified at <sha>") and no clause telling the sub-agent the brief may be wrong.

The load-bearing half is the no-fire side. Every dispatch the fleet makes passes
through here, so a matcher that fires on ordinary prose is noise a reader learns to
skip. Hence: a SHA shape needs a digit AND a letter (English words spell fine in the
hex alphabet — "defaced", "decade"), a config key needs an underscore (so the
``NEVER``/``ALWAYS`` imperatives every teatree brief carries are not keys), and
fenced blocks are excluded — a pasted command is an example, not the brief's own
assertion about the code.

Fail-safe-to-silent: empty or non-string input yields ``None``.
"""

import re
from typing import Final, NamedTuple

#: The remedy, quoted verbatim in the warning so the fix is copy-paste.
TRUST_THE_CODE_CLAUSE: Final[str] = (
    "If the review or this brief contradicts the code, TRUST THE CODE and say I was wrong."
)

_FENCED_CODE_RE: Final[re.Pattern[str]] = re.compile(r"```.*?```", re.DOTALL)

# A SHA shape carries at least one digit and one letter, which no English word in
# the hex alphabet does and no bare count does either.
_SHA = r"\b(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b"

_ASSERTION_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("file:line", re.compile(r"\b[\w./-]+\.[A-Za-z]{1,5}:\d+\b")),
    ("commit SHA", re.compile(_SHA)),
    (
        "count",
        re.compile(
            r"\bthere (?:are|is|were|was) (?:only |exactly |currently |now )?\d+\b|\b(?:exactly|precisely) \d+\b",
            re.IGNORECASE,
        ),
    ),
    (
        "non-existence",
        re.compile(
            r"\b(?:does|do|did|will)\s*n[o']?t\s+exist\b|\bthere (?:is|are|was|were) no\b|"
            r"\bno such\b|\bnever (?:existed|created)\b|\bis (?:absent|missing)\b",
            re.IGNORECASE,
        ),
    ),
    ("config key", re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")),
)

# A VERIFICATION word within one clause of a SHA: the brief pinned its claims to a
# commit someone read, so a sub-agent can tell for itself whether they still hold.
# Naming a ref ("the baseline is <sha>") is not one — that is the bare assertion.
_SHA_ANCHOR_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:verified|verify|confirmed|checked|read|observed|measured|as of|at|against)\b"
    rf"[^.\n]{{0,40}}?{_SHA}",
    re.IGNORECASE,
)

_TRUST_THE_CODE_RE: Final[re.Pattern[str]] = re.compile(
    r"\btrust the code\b|\bthe code (?:wins|is (?:the )?(?:authority|truth|source of truth))\b|"
    r"\bcontradicts? the code\b|\bfollow what the code (?:actually )?does\b|"
    r"\b(?:brief|prompt|review|context|description)\b[^.\n]{0,40}\b(?:may|might|could) be "
    r"(?:stale|wrong|outdated|out of date|incorrect)\b|"
    r"\bsay (?:that )?i (?:was|am) wrong\b|\b(?:if|where|when)\b[^.\n]{0,40}\bgot (?:this|it) wrong\b|"
    # Bare "say so" / "not a given" are ordinary English every long brief carries — the
    # licence must name what it applies to, or the clause check clears vacuously.
    r"\b(?:facts?|claims?|assertions?|premises?)\b[^.\n]{0,60}\b(?:as (?:a )?claims?|not (?:a )?givens?|to verify)\b|"
    r"\bverify (?:every|each|all|the) (?:fact|claim|assertion|premise)|"
    r"\b(?:do not|don'?t) trust (?:this|the) brief\b|\boverrule (?:this|the) brief\b",
    re.IGNORECASE,
)

_MAX_SAMPLES: Final[int] = 4


class BriefVerdict(NamedTuple):
    """What the brief asserted with nothing anchoring it: the shapes, and one sample each."""

    kinds: list[str]
    samples: list[str]


def _is_anchored(text: str) -> bool:
    """True when the brief pins its claims to a commit or licenses the sub-agent to overrule it."""
    return bool(_SHA_ANCHOR_RE.search(text) or _TRUST_THE_CODE_RE.search(text))


def find_unanchored_assertions(prompt: str) -> BriefVerdict | None:
    """Return a verdict when *prompt* asserts specifics with no anchor, else ``None``.

    Anchors are looked for in the WHOLE prompt (a clause inside a fence still
    licenses the sub-agent); assertions only outside fenced blocks.
    """
    if not isinstance(prompt, str) or not prompt.strip() or _is_anchored(prompt):
        return None
    prose = _FENCED_CODE_RE.sub(" ", prompt)
    kinds: list[str] = []
    samples: list[str] = []
    for kind, pattern in _ASSERTION_PATTERNS:
        match = pattern.search(prose)
        if match is None:
            continue
        kinds.append(kind)
        if len(samples) < _MAX_SAMPLES:
            samples.append(match.group(0).strip())
    if not kinds:
        return None
    return BriefVerdict(kinds=kinds, samples=samples)


def format_warning(verdict: BriefVerdict) -> str:
    """Render the advisory naming what fired and quoting the one-sentence remedy."""
    seen = "\n".join(f"  - {kind}: {sample!r}" for kind, sample in zip(verdict.kinds, verdict.samples, strict=False))
    return (
        "BRIEF-ANCHOR LINT — this dispatch asserts verifiable specifics that nothing in the "
        f"brief anchors:\n{seen}\n"
        f"({', '.join(verdict.kinds)} — no SHA anchor, no trust-the-code clause.)\n"
        "The sub-agent cannot tell which of these is stale, so it will build on all of them. "
        "Add one sentence to the brief:\n"
        f'  "{TRUST_THE_CODE_CLAUSE}"\n'
        "or anchor the claims to a commit you actually read (`verified at <sha>`)."
    )
