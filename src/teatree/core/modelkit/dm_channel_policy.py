"""Push-vs-pull routing for the owner DM — the deny-by-default interruption gate (#4524).

A DM interrupts a person, so it is the right vehicle for exactly one class of message:
something needs the owner's decision and nothing proceeds without it. Everything else
teatree emits — sweep skips, backlog ages, CI and PR states, watchdog findings — is
material the owner should be able to go and READ when they choose, and it is unbounded
in volume, so it reliably drowns the class that does.

:class:`~teatree.core.modelkit.notify_policy.NotifyAudience` answers "who is this for"
and denies ``INTERNAL`` by default. It cannot answer "does this deserve an interruption",
because the four owner audiences are declared per call site: writing
``audience=OWNER_ESCALATION`` ships a DM, so adding a new alarm is cheaper than asking
whether it earns one. This module is the missing half — one classifier the egress
consults, with :attr:`DmChannel.PULL` as the answer for every signal not named in
:data:`PUSH_SIGNALS`. A new alarm therefore cannot reach the DM by being *written*; it
reaches it only by being *registered here*, which is the conversation the gate exists
to force.

A key is PUSH iff one of its colon-delimited prefixes is registered, so a registrant
picks the granularity it needs: ``watchdog:compose-up-failed`` pages for a dead stack
while ``watchdog:red`` — 225 of one 7-day window's 666 DMs, all reporting one recurring
box condition — stays pull.

Keying on the idempotency key is what lets the gate cover callers that declare nothing:
the shell reaches this egress through ``t3 <overlay> notify send``, whose audience is
fixed, and the leading segment is the naming convention every existing call site already
follows. Zero-dependency by construction (:mod:`enum` only) — ``teatree.core.modelkit``
is a ``depends_on = []`` tach node, so the classifier stays a pure function the egress,
the CLI and the tests all reach with no cycle and no Django settings.
"""

import enum

from teatree.core.modelkit.notify_policy import NotifyAudience


class DmChannel(enum.StrEnum):
    """Whether a notification interrupts the owner or waits to be read."""

    PUSH = "push"
    PULL = "pull"


#: The CLOSED allowlist of status signals that earn an interruption. Only the outage
#: class lives here: a decision the owner must make reaches them as an
#: ``OWNER_QUESTION``, which pushes on its audience alone and needs no entry.
#:
#: Adding a slug is a deliberate act — it says this signal is worth waking someone for.
PUSH_SIGNALS: frozenset[str] = frozenset(
    {
        # The compose stack is down and the watchdog could not bring it back up.
        "watchdog:compose-up-failed",
        # The box is unreachable, or its doctor is crashing — both need a human on it.
        "watchdog:doctor-unreachable",
        "watchdog:doctor-no-verdict",
        # The claim path is refusing new work until a human runs the pending
        # migrations — the factory is parked, not merely degraded (#4524 review).
        "schema_behind_code",
    }
)

#: Audiences that interrupt on their own terms, whatever signal carries them: a question
#: is by definition a blocked decision, and a colleague-facing act is the receipt the
#: on-behalf discipline owes the owner.
_ALWAYS_PUSH_AUDIENCES: frozenset[NotifyAudience] = frozenset(
    {NotifyAudience.OWNER_QUESTION, NotifyAudience.COLLEAGUE_ACTION}
)


def signal_prefixes(idempotency_key: str) -> tuple[str, ...]:
    """Every colon-delimited prefix of *idempotency_key*, shortest first."""
    if not idempotency_key:
        return ()
    segments = idempotency_key.split(":")
    return tuple(":".join(segments[: i + 1]) for i in range(len(segments)))


def signal_of(idempotency_key: str) -> str:
    """The signal a key belongs to — its leading segment, the digest's grouping key."""
    return idempotency_key.split(":", 1)[0]


def classify(*, audience: NotifyAudience, idempotency_key: str) -> DmChannel:
    """Whether this notification may interrupt the owner. Unregistered means PULL."""
    if audience in _ALWAYS_PUSH_AUDIENCES:
        return DmChannel.PUSH
    if audience == NotifyAudience.INTERNAL:
        return DmChannel.PULL
    if PUSH_SIGNALS.intersection(signal_prefixes(idempotency_key)):
        return DmChannel.PUSH
    return DmChannel.PULL


__all__ = ["PUSH_SIGNALS", "DmChannel", "classify", "signal_of", "signal_prefixes"]
