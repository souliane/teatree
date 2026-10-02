"""``[quote-ok: <reason>]`` opt-out recognition for the pre-dispatch gate (#1401).

Split out of :mod:`teatree.hooks.quote_scanner` (at its per-file LOC ceiling) so
the token's placement contract lives in one readable leaf. Pure stdlib, so the
cold PreToolUse hook subprocess can import it.

**Why a positional window alone was unusable for the ``prompt`` field.** The
token was recognised only inside the first 512 characters of the joined
dispatch payload, borrowed from the router's own in-command skip tokens. That
window governs a short command string; a dispatch brief is not one.
``/t3:rules`` § "Sub-Agent Limitations" REQUIRES every raw ``Agent``/``Task``
spawn to carry the ``t3 <overlay> skill-preamble`` output — several kilobytes of
embedded ``SKILL.md`` bodies — PREPENDED to the brief. So the authored brief
starts far past character 512, and a token written where an earlier version of
this gate's refusal said to write it ("near the start of the prompt") was
silently inert: the agent added the escape, got refused again, and had no
signal that the escape had not been seen.

**Why the tried fix (own-line, unwindowed) was worse than the friction it
solved.** A prior revision recognised the token anywhere in the payload
provided it stood alone on its own line, reasoning that no pasted paragraph or
quoted sentence produces a STANDALONE line by accident. That reasoning holds
for prose but not for a copied document: a fenced code block, a pasted log, or
relayed meeting notes routinely carry a line with nothing else on it, and any
such line matching the token shape then authorised the WHOLE dispatch —
whatever HIGH-severity content sat elsewhere in the same brief. A quoted token
merely present in content the brief relays is not an authoring act, and
recognising it as one is a privilege escalation, not a placement fix.

**What actually works.** The escape stays windowed to the first
:data:`TOKEN_WINDOW` characters of EACH field it is checked against — no
unbounded scan, own-line or otherwise. The ``description`` (subject) field is
always short enough to sit entirely inside its own window regardless of how
long the ``prompt`` is, so it is the escape that survives a multi-kilobyte
preamble: an author who needs to authorise a long brief writes the token in
``description``, not buried past the window in ``prompt``. A token beyond the
window in ``prompt`` — including one that merely sits on its own line inside
relayed content — does not authorise. The reason stays mandatory and every
sanctioned dispatch is still written to the gate's ledger, so the opt-out
remains auditable rather than silent.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Final

# Characters of a single field within which the token is recognised wherever it
# sits. Unchanged from the pre-#1401-friction rule, so no brief that authorises
# today stops authorising.
TOKEN_WINDOW: Final[int] = 512

# The harness names the sub-agent dispatch vehicle ``Agent`` or ``Task``; both
# carry a ``prompt`` (the dispatched brief) and a short ``description`` (the
# one-line subject), in that order.
DISPATCH_PROMPT_FIELDS: Final[tuple[str, ...]] = ("description", "prompt")

# ``[quote-ok: <reason>]`` — the reason is MANDATORY (``\S`` opens it), so an
# empty reason never bypasses and an audit can read WHY a quote-shaped dispatch
# was sanctioned. Mirrors ``hook_router``'s ``[skill-load-ok: <reason>]``.
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\[quote-ok:\s*(\S[^\]]*?)\s*\]")


def _reason_from(match: re.Match[str] | None) -> str | None:
    return (match.group(1).strip() or None) if match else None


def reason_in(text: str) -> str | None:
    """Return the ``[quote-ok: <reason>]`` reason authorised by ``text``, else ``None``.

    Recognised ONLY inside the first :data:`TOKEN_WINDOW` characters of
    ``text`` — never anywhere past it, own-line or otherwise. See the module
    docstring for why an unbounded scan is unsafe: it lets a token merely
    quoted inside pasted/relayed content authorise the whole dispatch.
    """
    if not text:
        return None
    return _reason_from(_TOKEN_RE.search(text[:TOKEN_WINDOW]))


def reason_in_fields(values: Iterable[str]) -> str | None:
    """Return the reason authorised by ANY ONE dispatch field, else ``None``.

    Each field gets its OWN window rather than sharing one across the joined
    payload, so a long ``description`` can never consume the ``prompt``'s budget
    (or the reverse). The first field that authorises wins.
    """
    for value in values:
        reason = reason_in(value)
        if reason is not None:
            return reason
    return None


def block_message(names: str, excerpt: str) -> str:
    """The #1401 dispatch refusal, whose escape half names a placement that WORKS.

    The old wording ("near the start of the prompt") pointed at a location the
    mandated ``skill-preamble`` prefix pushes out of the recognition window, so
    the token was added and silently ignored — the refusal taught the agent
    something untrue. The ``description`` field named here is the one placement
    that is BOTH recognised by :func:`reason_in` and immune to preamble length,
    and the relayed-authorisation case is called out because it is a legitimate
    reason an agent otherwise has no way to know is allowed.
    """
    matched = f' (e.g. "{excerpt}")' if excerpt else ""
    return (
        "BLOCKED: pre-dispatch quote-scanner gate (#1401). The Agent/Task prompt "
        f"carries verbatim user-voice/PII content{matched} — matched patterns: {names}. "
        "Paraphrase it into author-voice description before dispatching (the sub-agent "
        "would otherwise echo it into a published output, defeating the #1213 publish gate). "
        "If the match is a false positive — for example you are relaying the owner's own "
        "authorisation verbatim into the brief, which is inbound, not published — add "
        "`[quote-ok: <reason>]` to the one-line `description` (subject) field, which always "
        f"gets its own {TOKEN_WINDOW}-character window regardless of prompt length. A token "
        f"placed more than {TOKEN_WINDOW} characters into the `prompt` field itself is NOT "
        "read — an embedded skill preamble routinely pushes the top of your brief past that "
        "point, and a token merely quoted inside pasted/relayed content must never authorise "
        "the whole dispatch. "
        "Example subject: `<subject> [quote-ok: relaying the owner's authorisation verbatim "
        "to the sub-agent]`."
    )


def field_values(tool_input: Mapping[str, object]) -> list[str]:
    """The populated dispatch-prompt field values, in :data:`DISPATCH_PROMPT_FIELDS` order."""
    values: list[str] = []
    for name in DISPATCH_PROMPT_FIELDS:
        value = tool_input.get(name)
        if isinstance(value, str) and value:
            values.append(value)
    return values
