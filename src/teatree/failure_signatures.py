"""Phrase tables and pure text predicates that classify WHY a run failed.

The evidence is always text — a dead sub-agent's result envelope, a review
backend's stderr, a FAILED attempt's stored ``error`` — and its readers sit on
opposite sides of the module graph: the recorder chokepoint in
:mod:`teatree.agents.attempt_recorder`, the cooldown seam in
:mod:`teatree.core.review.backend_cooldown`, the requeue sweep in
:mod:`teatree.loop.transient_requeue`. ``teatree.agents`` and ``teatree.core``
share the ``domain`` layer and so cannot import each other; the tables and the
predicates over them therefore live in this dependency-free foundation leaf,
which every reader depends on downward.

Precision over recall: an outage death is rare relative to legit completions, so
a false positive (failing a genuine completion that merely *mentions* an API
error) is worse than a missed one. :func:`outage_signature_in_text` keys on
connection signatures that do not occur in normal phase-completion prose, and
treats the bare phrase "API Error" as outage ONLY when it co-occurs with a
connection phrase — a summary like "added API error handling" is not an outage.

:func:`quota_exhausted` applies the same discipline to a different question — did
a review BACKEND run out of capacity? — where the phrases ("rate limit", "429")
DO occur in normal prose, so the precision comes from scoping what may be scanned
rather than from the phrase list.
"""

_CONNECTION_SIGNATURES = (
    "unable to connect to api",
    "connectionrefused",
    "connection refused",
    "failedtoopensocket",
    "failed to open socket",
    "safety classifier unavailable",
)

_API_ERROR_PHRASE = "api error"

_CONNECTION_COOCCURRENCE_PHRASES = (
    "connect",
    "socket",
    "network",
    "timed out",
    "timeout",
    "unreachable",
    "refused",
    "reset by peer",
)


def outage_signature_in_text(text: str) -> str:
    """Return the matched outage signature in *text*, or ``""`` for no outage.

    A direct connection signature returns itself; the generic "API Error" phrase
    counts only when a connection phrase co-occurs, returning
    ``"api error + <phrase>"``. Case-insensitive.
    """
    haystack = text.casefold()
    for signature in _CONNECTION_SIGNATURES:
        if signature in haystack:
            return signature
    if _API_ERROR_PHRASE in haystack:
        for phrase in _CONNECTION_COOCCURRENCE_PHRASES:
            if phrase in haystack:
                return f"{_API_ERROR_PHRASE} + {phrase}"
    return ""


#: Phrases a review backend emits on its own stderr when the ACCOUNT is out of
#: capacity — the run failed for want of quota, not for anything about the diff.
#: Same precision-over-recall discipline as :data:`_CONNECTION_SIGNATURES`, and it
#: needs it more: every one of these phrases occurs naturally in a REVIEW BODY
#: (a finding about missing backoff, a 429 in a pasted trace). The discipline is
#: enforced by WHERE the classifier is allowed to look, not by the phrase list —
#: see :func:`quota_exhausted`.
_QUOTA_SIGNATURES = (
    "usage limit",
    "rate limit",
    "quota",
    "insufficient credit",
    "plan limit",
    "429",
)


def quota_signature(text: str) -> str:
    """Return the matched quota-exhaustion signature in *text*, or ``""``.

    A raw substring scan, case-insensitive. It says only "this text contains an
    exhaustion phrase" — it does NOT say a run hit its quota, because these
    phrases are ordinary review prose. :func:`quota_exhausted` is the predicate
    that answers the real question; call that unless you are the one deciding
    what text is even eligible to be scanned.
    """
    haystack = text.casefold()
    for signature in _QUOTA_SIGNATURES:
        if signature in haystack:
            return signature
    return ""


def quota_exhausted(*, returncode: int, stderr: str) -> str:
    """The quota signature of a FAILED backend run, or ``""``.

    Two scoping rules keep :data:`_QUOTA_SIGNATURES` from firing on ordinary
    review prose, and they are why this predicate exists rather than callers
    scanning text themselves. A zero *returncode* is never exhaustion — the run
    did its work, whatever its output says. And only *stderr* is eligible —
    never the review body, whose findings routinely discuss rate limits and 429s.

    Getting either wrong costs hours of a cooled-down backend over a job that
    succeeded, so the decision lives here and is pinned by a false-positive test.
    """
    if returncode == 0:
        return ""
    return quota_signature(stderr)


# Namespaced markers a FAILED attempt's ``error`` carries when the death was an
# infrastructure interruption rather than a deterministic defect. Each is emitted
# by exactly one recording seam: ``outage_death:`` by the recorder (#1764),
# ``result_error:`` by the headless driver for the #1764 "genuine FAILED run"
# class (a missing terminal ResultMessage OR an ``is_error`` result — both
# transient), ``provision_failed:`` by a worktree/provisioning step, and
# ``landing_unverified:`` by the completion chokepoint when a coder yielded
# without committing. A deterministic refusal (evidence gate, schema, review
# verdict, a real assertion/test failure, a ``stuck_loop`` runaway) matches none.
_TRANSIENT_MARKERS = (
    "outage_death:",
    "result_error:",
    "provision_failed:",
    "landing_unverified:",
)


def transient_failure_signature(error: str) -> str:
    """Return the transient signature of a FAILED attempt's *error*, or ``""``.

    A non-empty return means the failure was an infrastructure interruption the
    bounded auto-requeue sweep MAY reopen; ``""`` means a deterministic failure
    that must stay terminal FAILED. Keys on the namespaced markers above, plus a
    raw connection / "API Error + connection" signature in the error text (an
    outage death whose envelope was never stamped with the ``outage_death:``
    prefix). Case-insensitive.
    """
    haystack = error.casefold()
    if not haystack.strip():
        return ""
    for marker in _TRANSIENT_MARKERS:
        if marker in haystack:
            return marker.rstrip(": ")
    return outage_signature_in_text(error)


def is_transient_failure(error: str) -> bool:
    """Whether a FAILED attempt's *error* classifies as a transient interruption."""
    return bool(transient_failure_signature(error))
