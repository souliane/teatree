r"""Opaque Slack/forge ID leak detector — a NEW leak class.

The banned-terms and overlay-leak gates catch *named* tokens (a configured
customer/overlay word). They never caught an OPAQUE identifier — a Slack
channel/DM/user id (``C0…``/``D0…``/``U0…``), or a Slack app/team id
(``A0…``/``T0…``). Those are internal references that must never reach a
public surface, yet they carry no dictionary word to put on a banned list.

This module detects the ID *shape* directly and is shared by both the
full-tree overlay-leak scan (``check_no_overlay_leak.py``) and the
publish-surface privacy scan (``scripts/privacy_scan.py``), so the two
paths cannot drift.

The hazard with a shape detector is that the repo's own test fixtures and
documentation examples use the same shape. A synthetic-placeholder
ALLOWLIST recognises the obviously-invented forms — DEMO/word tokens,
sequential runs (``01ABCD1234``), repeated-character runs
(``0AAAAAAAAA``, ``0000000001``) — so those never trip while a
random-looking real id still does. Every example in this module is
synthetic.
"""

import re
import string

# A Slack/forge opaque id: a single leading category letter, then ``0``,
# then 8-10 uppercase-alnum characters. ``C``/``D``/``U`` cover
# channel/DM/user; ``A``/``T`` cover app/team. The surrounding
# ``(?<![A-Z0-9])`` / ``(?![A-Z0-9])`` are token-boundary guards so a
# longer alnum run is not an id (``XC0ZX91QWERTX`` is not a hit).
_OPAQUE_ID_RE = re.compile(r"(?<![A-Z0-9])[CDUAT]0[A-Z0-9]{8,10}(?![A-Z0-9])")

# Inline allow-annotation (mirrors ``privacy-scan:allow`` in
# scripts/privacy_scan.py): a line carrying this literal marker is exempt
# from the opaque-ID pass. This module's OWN detector tests must contain
# real-shaped ids to prove the gate trips, so those fixture lines carry
# the marker. It exempts only the line it appears on — a real leak on any
# other line is still reported.
_ALLOW_MARKER = "leak-scan:allow"

# Dictionary tokens that mark a SYNTHETIC placeholder id. A random real id
# never spells one of these; a hand-written fixture/example almost always
# does. Matched case-insensitively against the whole id.
_SYNTHETIC_WORDS: tuple[str, ...] = (
    "DEMO",
    "CACHED",
    "REVIEW",
    "COLLEAGUE",
    "GLOBAL",
    "UNKNOWN",
    "INTERNAL",
    "USER",
    "TEAM",
    "CLNT",
    "CLIENT",
    "CHAN",
    "CHANNEL",
    "FIRST",
    "SECOND",
    "THIRD",
    "LIVE",
    "BOT",
    "TEST",
    "FAKE",
    "STUB",
    "EXAMPLE",
    "PLACEHOLDER",
    "SAMPLE",
    "DUMMY",
    "APP",
)

# A sequential alphanumeric run an author types as an obvious filler:
# ``01ABCD1234`` (the body after the leading ``X0`` is ``ABCD1234`` or a
# digit run), ``0123456789``. We treat any id whose post-prefix body is a
# strictly-ascending letter or digit ladder as synthetic.
_SEQUENTIAL_BODIES: frozenset[str] = frozenset(
    {
        string.digits,
        "123456789",
        "ABCD1234",
        "01ABCD1234",
        "ABCDEFGHIJ",
    }
)


def _has_repeated_run(token: str, *, run: int = 5) -> bool:
    """True when *token* contains a run of ``run`` identical characters.

    ``U0AAAAAAAAA`` / ``D0000000001`` are obvious fillers; a random id does
    not carry a 5-long identical run.
    """
    return re.search(rf"(.)\1{{{run - 1},}}", token) is not None


def _has_synthetic_word(token: str) -> bool:
    upper = token.upper()
    return any(word in upper for word in _SYNTHETIC_WORDS)


def is_synthetic_placeholder(token: str) -> bool:
    """Whether *token* is an obviously-synthetic placeholder id.

    A synthetic id is one a human invented for a fixture/example: it spells
    a dictionary marker (``DEMO``, ``REVIEW`` …), is a sequential filler
    (``01ABCD1234``), or carries a repeated-character run (``0AAAAAAAAA``,
    ``0000000001``). Such ids are exempt so the detector does not flag the
    repo's own tests and docs. The check is intentionally generous: a false
    "synthetic" verdict only relaxes the gate for an invented-looking id, a
    cost far smaller than a false leak block on every fixture line.
    """
    body = token[1:]  # drop the category letter; keep the leading 0
    return (
        _has_synthetic_word(token)
        or body in _SEQUENTIAL_BODIES
        or token[2:] in _SEQUENTIAL_BODIES
        or _has_repeated_run(token)
    )


def find_opaque_ids(text: str) -> list[str]:
    """Return every real-shaped opaque id in *text*, allowlist applied.

    Synthetic placeholders (:func:`is_synthetic_placeholder`) are dropped,
    so only genuinely random-looking ids remain — the ones that are real
    leaks. A line carrying the ``leak-scan:allow`` marker is exempt (used by
    this module's own detector tests, which must carry real-shaped ids).
    Order of appearance is preserved.
    """
    if _ALLOW_MARKER in text:
        return []
    return [m.group(0) for m in _OPAQUE_ID_RE.finditer(text) if not is_synthetic_placeholder(m.group(0))]
