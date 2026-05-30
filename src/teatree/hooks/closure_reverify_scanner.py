r"""Closure-verb re-verify advisory (#1448).

In long autonomous sessions the orchestrator has restated a closure verdict
— "merged #N", "closed !N", "confirmed superseded", "X landed" — *without*
verifying the id's live state in the same turn, twice in recent recurrences.
The artifact stays in its pre-decision state and the user re-asks many times
before the action actually fires.

This module is the deterministic detector that the Stop handler in
``hooks/scripts/hook_router.py`` uses to surface a non-blocking WARN when a
HIGH-confidence closure claim re-cites an id and NO same-turn state check on
that id is present. It is a pure-detection sibling of the bare-reference and
consideration Stop advisories.

WARN-only by design (issue #1448 + the #1567 deadlock precedent): a
turn-inspecting gate that hard-blocks on fuzzy prose is dangerous — a sibling
skill-loading gate over-fired and deadlocked the loop. So the PRIME directive
is to never false-fire on a legitimate, already-verified, or merely-narrative
closure. The detector is therefore tuned hard for precision over recall: when
in doubt it stays SILENT.

``find_unverified_closures`` FIRES only on a closure claim — an assertive
closure verb (``merged`` / ``closed`` / ``resolved`` / ``landed`` /
``superseded`` / "I'll close" / "I've closed") within ``_PROXIMITY`` chars of
an id (``#\d+`` / ``!\d+`` / ``PR \d+`` / ``MR \d+``) — for an id that has NO
same-turn state check.

It MUST NOT fire on (the load-bearing no-fire guards): a same-turn
state-check tool_use touching that id (``gh pr view <id>`` / ``glab mr view``
/ ``gh issue view`` / ``gh api`` / a merge-ceremony CLI / a ``git`` state
read) — the agent verified, so ``state_checked_ids`` drops it; a narrative /
past-tense mention that is not a claim about *this turn's* outcome ("the bug
fixed in #N last week", "builds on the merged #N", "as discussed in #N"),
which ``_is_narrative_context`` excises; a closure verb with no id at all
("fixed the typo"); and the merge-ceremony's own output — that IS the
verification, so the same-turn state-check clears it.

Fail-OPEN everywhere: a bug here must never wedge turn-end. The caller wraps
the whole thing in a crash-proof ``try`` and the detector returns ``[]`` on
empty/odd input.
"""

import re
from typing import Final

# An id reference shape. ``PR``/``MR`` spelled-out forms are normalised to the
# ``#``/``!`` token so "PR 42" and "#42" verify against the same tool_use.
_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w/])(?:(?P<hash>[#!]\d+)|(?:(?P<kind>PR|MR)\s+(?P<num>\d+)))",
    re.IGNORECASE,
)

# Assertive closure verbs — a *claim* that the artifact reached a terminal
# state, or a first-person promise to put it there. Tuned for precision: only
# the verbs the recurrences actually used. ``done`` is deliberately excluded —
# it is far too common in narrative status prose to carry an id-closure claim
# safely, and the issue's own regex over-broadly includes it.
_CLOSURE_VERB_RE: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"merged|closed|closing|resolved|landed|superseded|"
    r"i'?ll close|i'?ve closed|i'?ll merge|i'?ve merged|"
    r"is (?:now )?(?:closed|merged|resolved)|"
    r"has (?:been )?(?:closed|merged|resolved|landed)|"
    r"have (?:been )?(?:closed|merged|resolved|landed)"
    r")\b",
    re.IGNORECASE,
)

# Max distance between a closure verb and an id for them to be one claim.
_PROXIMITY: Final[int] = 60

# Narrative lead-ins: phrasing that frames an id mention as a *reference to an
# earlier/other* artifact, not a claim about this turn's outcome. When one of
# these immediately precedes the id (within a short window) the span is
# excised before claim matching, so "builds on the merged #N" / "the bug fixed
# in #N last week" / "as discussed in #N" never fire.
_NARRATIVE_LEAD_RE: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"builds on|build on|building on|"
    r"based on|on top of|follows? (?:from|on)|follow-?up to|followup to|"
    r"as (?:discussed|noted|mentioned|described|seen) in|"
    r"discussed in|noted in|mentioned in|described in|tracked in|"
    r"see |refer to|reference[ds]? in|per |fixed in|landed in|merged in"
    r")\b[^.?!\n]{0,40}$",
    re.IGNORECASE,
)

# Past-time trailing markers ("… #N last week", "… #N yesterday") that frame
# the mention as historical narrative rather than a fresh closure claim.
_PAST_TIME_TRAILER_RE: Final[re.Pattern[str]] = re.compile(
    r"^[^.?!\n]{0,40}\b("
    r"last (?:week|month|sprint|cycle|time|year)|"
    r"yesterday|previously|already|earlier|before|back then|"
    r"a (?:while|few days|few weeks) ago|ages ago"
    r")\b",
    re.IGNORECASE,
)


def _normalise_id(match: re.Match[str]) -> str:
    """Return the canonical id token (``#42`` / ``!42``) for an id match."""
    hash_token = match.group("hash")
    if hash_token:
        return hash_token
    kind = (match.group("kind") or "").upper()
    sigil = "!" if kind == "MR" else "#"
    return f"{sigil}{match.group('num')}"


def _is_narrative_context(text: str, id_start: int, id_end: int) -> bool:
    """True when the id at ``[id_start, id_end)`` reads as a narrative mention.

    The id is a reference to an earlier/other artifact — not a claim about
    this turn's closure — when a narrative lead-in immediately precedes it
    (``builds on …``, ``as discussed in …``, ``fixed in …``) or a past-time
    marker immediately follows it (``… last week``). Both windows are short
    so an unrelated earlier sentence cannot suppress a genuine claim.
    """
    before = text[:id_start]
    after = text[id_end:]
    return bool(_NARRATIVE_LEAD_RE.search(before) or _PAST_TIME_TRAILER_RE.search(after))


def find_closure_claims(text: str) -> list[str]:
    """Return canonical ids that carry a HIGH-confidence closure claim.

    An id is a closure claim when an assertive closure verb sits within
    ``_PROXIMITY`` chars of it (either side) AND the id is not in a narrative
    context. Returns a de-duplicated, order-preserving list. Empty text or no
    match yields ``[]`` (the caller then does nothing).
    """
    if not text:
        return []
    claims: list[str] = []
    seen: set[str] = set()
    for match in _ID_RE.finditer(text):
        token = _normalise_id(match)
        if token in seen:
            continue
        if _is_narrative_context(text, match.start(), match.end()):
            continue
        window_start = max(0, match.start() - _PROXIMITY)
        window = text[window_start : match.end() + _PROXIMITY]
        if _CLOSURE_VERB_RE.search(window):
            seen.add(token)
            claims.append(token)
    return claims


# State-read verbs that count as same-turn verification of an id's live state:
# a forge view (``gh pr view`` / ``glab mr view`` / ``gh issue view``), a forge
# API read (``gh api`` / ``glab api``), a merge-ceremony CLI (``t3 … merge`` —
# its run IS the verification), or a ``git`` state read. ``gh pr merge`` /
# ``glab mr merge`` / ``gh pr close`` etc. are themselves the executing action,
# so they also count as touching the id's live state. The capture group binds
# the bare numeric id that the forge subcommand addresses (``gh pr view 1448``)
# — these tools take the id as a bare number, not a ``#N`` token. ``mr_kind``
# disambiguates the GitLab MR sigil (``!N``) from the GitHub PR/issue (``#N``).
_STATE_CHECK_RE: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"gh (?:pr|issue) (?:view|merge|close|edit)\s+(?P<gh_num>\d+)|"
    r"glab (?P<mr_kind>mr|issue) (?:view|merge|close|update)\s+(?P<glab_num>\d+)|"
    r"gh api[^\n]*?/(?:pulls|issues)/(?P<api_num>\d+)|"
    r"(?:--pr|--issue)\s+(?P<pr_flag_num>\d+)|"
    r"--mr\s+(?P<mr_flag_num>\d+)|"
    r"(?:gh api|glab api|\bt3\b[^\n]*\bmerge\b|merge-ceremony|"
    r"git (?:log|status|show|branch|rev-parse|ls-remote|fetch))"
    r")",
    re.IGNORECASE,
)


def _id_tokens_in(text: str) -> set[str]:
    """Canonical id tokens (``#N`` / ``!N`` / ``PR N``) appearing in ``text``."""
    return {_normalise_id(m) for m in _ID_RE.finditer(text)}


def _state_check_ids_in(command: str) -> set[str]:
    """Ids a state-read in ``command`` addresses, normalised to ``#N`` / ``!N``.

    Two id surfaces are credited: the ``#N`` / ``PR N`` / ``MR N`` tokens the
    free-text claim would also use (via ``_id_tokens_in``), and the BARE
    numeric id a forge subcommand takes (``gh pr view 1448`` →\u200b ``#1448``,
    ``glab mr view 1448`` →\u200b ``!1448``). Without a state-read verb, returns
    ``set()`` — a bare number elsewhere never counts as verification.
    """
    found: set[str] = set()
    for match in _STATE_CHECK_RE.finditer(command):
        gh_num = match.group("gh_num")
        if gh_num:
            found.add(f"#{gh_num}")
        glab_num = match.group("glab_num")
        if glab_num:
            sigil = "!" if (match.group("mr_kind") or "").lower() == "mr" else "#"
            found.add(f"{sigil}{glab_num}")
        api_num = match.group("api_num")
        if api_num:
            found.add(f"#{api_num}")
        pr_flag_num = match.group("pr_flag_num")
        if pr_flag_num:
            found.add(f"#{pr_flag_num}")
        mr_flag_num = match.group("mr_flag_num")
        if mr_flag_num:
            found.add(f"!{mr_flag_num}")
    if found or _STATE_CHECK_RE.search(command):
        found |= _id_tokens_in(command)
    return found


def state_checked_ids(tool_commands: list[str]) -> set[str]:
    """Canonical ids that a same-turn state-check tool_use touched.

    ``tool_commands`` is the flattened text of every tool_use in the turn
    (Bash ``command`` / Agent-or-Task ``prompt`` + ``description``). An id is
    "verified" when it co-occurs with a state-read verb in the SAME command
    string — that pairs the read to the specific id rather than crediting an
    unrelated read elsewhere in the turn.
    """
    verified: set[str] = set()
    for command in tool_commands:
        if command:
            verified |= _state_check_ids_in(command)
    return verified


def find_unverified_closures(text: str, tool_commands: list[str]) -> list[str]:
    """Return closure-claim ids with NO same-turn state check (the fire set).

    Empty when every claim was verified in the same turn, when no claim is
    present, or when the text carries no id at all. This is the WARN trigger.
    """
    claims = find_closure_claims(text)
    if not claims:
        return []
    verified = state_checked_ids(tool_commands)
    return [token for token in claims if token not in verified]


def format_warn_message(ids: list[str]) -> str:
    """Render the non-blocking advisory for ``ids`` (never blocks the turn)."""
    listed = ", ".join(ids)
    return (
        f"ADVISORY: closure-verify (#1448) — this turn claims a closure for {listed} "
        "but no same-turn state check (e.g. `gh pr view`, `glab mr view`, `gh issue view`, "
        "the merge-ceremony CLI) was observed for it. If the artifact is actually closed, "
        "this is just a reminder; otherwise re-verify its live state or dispatch the action "
        "before claiming done."
    )
