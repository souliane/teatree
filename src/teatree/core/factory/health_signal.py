"""The observations the operational-health aggregator collects, and what it could not read."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HealthSignal:
    """One live "something is wrong" observation feeding the aggregator.

    *fingerprint* is the stable dedupe key — the same problem seen on two ticks
    carries the same fingerprint so it updates one :class:`KnownIssue` row
    rather than piling up duplicates. *severity* is a
    :class:`KnownIssue.Severity` value (``critical`` / ``warning``). *kind* is a
    coarse machine label for the signal family; *overlay* scopes it; *summary*
    is the human line; *evidence_url* is the clickable jump-to-proof link.
    """

    fingerprint: str
    severity: str
    summary: str
    kind: str = ""
    overlay: str = ""
    evidence_url: str = ""


@dataclass(frozen=True, slots=True)
class SignalCollection:
    """What a health read SAW, and which sources it could not read at all (#4354).

    ``signals`` are the live observations. ``unread`` names every source whose read
    FAILED — an overlay that raised, a DB query that errored, a whole collector that
    blew up. The two are kept apart because both a failed read and an all-clear read
    contribute zero signals, and :meth:`KnownIssueManager.reconcile` treats a missing
    fingerprint as RESOLVED: collapsing them retires an issue nothing has fixed.
    """

    signals: tuple[HealthSignal, ...] = ()
    unread: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """True iff every source answered, so an absent fingerprint really did clear."""
        return not self.unread
