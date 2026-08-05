"""Manager/queryset for :class:`SessionHandover` rows.

Split out of ``teatree.core.managers`` (mirrors the ``loop_lease_manager``
split) so the session-handover concern — creating a hand-off and the
drain CAS — lives in one self-describing module. ``managers``
re-exports the public symbols so ``from teatree.core.managers import …``
call sites are unchanged.

The claim is a backend-agnostic compare-and-swap (a conditional ``UPDATE``
gated on ``claimed_at IS NULL``), NOT ``select_for_update(skip_locked=True)``
— teatree's production DB is SQLite where that clause is silently dropped
(the #786 B1 lesson). Exactly one of N racing SessionStart hooks wins each row; a loser updates
0 rows for that row and moves on to the next claimable one.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.utils import timezone

from teatree.core.session_identity import is_loop_runner_session

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Sequence

    from teatree.core.models.session_handover import SessionHandover


def render_fenced_handoffs(entries: "Sequence[tuple[str, str, str]]") -> str:
    """Concatenate ``(author, iso_instant, payload)`` entries, each behind its own fence.

    A lone entry renders as its bare payload. Several render fenced, so a reader
    handed N authors' state never reads it as one narrative — the shape both the
    drain (:func:`teatree.core.handover.render_claimed_payload`) and the
    duplicate-collapse migration emit.
    """
    if len(entries) == 1:
        return entries[0][2]
    return "\n\n".join(
        f"## Hand-off {index} of {len(entries)} — from `{author}` at {instant}\n\n{payload}"
        for index, (author, instant, payload) in enumerate(entries, start=1)
    )


def _absorb_payload(*, prior: str, incoming: str, author: str, at: "dt.datetime") -> str:
    """*prior* with *incoming* appended behind a fence — never *incoming* alone.

    The one-row-per-session rule replaces the ROW, not the state it carries: the
    measured incident had 22,224 bytes of hand-off in flight, and a session that
    hands off twice is adding to what it already said, not retracting it. An
    incoming payload already present in *prior* (a re-run over the same snapshot)
    is dropped rather than duplicated.
    """
    if not incoming.strip() or incoming.strip() in prior:
        return prior
    if not prior.strip():
        return incoming
    return f"{prior.rstrip()}\n\n## Hand-off update — from `{author}` at {at.isoformat()}\n\n{incoming}"


@dataclass(frozen=True, slots=True)
class HandoverWrite:
    """What the write seam DID: the row, and whether it landed on an existing one.

    Reported from here rather than predicted by a pre-read in the caller. The caller
    used to read the row it expected to absorb into BEFORE the write, which is a
    different instant: a rival insert landing in between made the caller's pre-read
    empty while the write took the absorb branch via the ``IntegrityError`` retry, so
    a call that absorbed announced itself as a fresh insert. ``previous_bytes`` is
    captured inside :func:`_absorb`, from the row actually being absorbed into.
    """

    row: "SessionHandover"
    absorbed: bool
    previous_bytes: int


def _absorb(existing: "SessionHandover", *, to_session: str, payload: str) -> HandoverWrite:
    now = timezone.now()
    previous_bytes = len(existing.payload)
    existing.payload = _absorb_payload(prior=existing.payload, incoming=payload, author=existing.from_session, at=now)
    existing.to_session = to_session
    existing.created_at = now
    existing.save(update_fields=["to_session", "payload", "created_at"])
    return HandoverWrite(row=existing, absorbed=True, previous_bytes=previous_bytes)


class SelfAddressedHandoverError(ValueError):
    """A hand-off addressed to the session creating it, which nothing could ever claim.

    Raised by :meth:`SessionHandoverQuerySet.create_handover` so the degenerate row
    is refused where an operator can still react, rather than persisting as a row
    that counts as pending forever. See :meth:`~SessionHandoverQuerySet.claimable_for`
    for why such a row is unreachable.
    """


class SessionHandoverQuerySet(models.QuerySet):
    def create_handover(self, *, from_session: str, to_session: str, payload: str) -> HandoverWrite:
        """Persist the pending hand-off from ``from_session``, one row per session.

        ``to_session == ""`` targets "whichever session starts next". The
        row is the source of truth; the caller mirrors ``payload`` to the
        XDG file separately.

        A session gets ONE unclaimed row, ABSORBING each later hand-off. Repeated
        ``create`` calls used to insert siblings, so a session that learned
        something after its first hand-off left the receiver several rows to
        reconcile — one session produced three in fourteen minutes, each
        superseding the last in prose ("Supersedes the previous addendum. Read all
        three."). The receiver cannot diff prose, so the newest row is not reliably
        the whole story. The later payload is appended behind a fence rather than
        written over the earlier one: replacing the ROW must not replace the STATE.
        ``created_at`` is refreshed so the parked tier's oldest-first delivery order
        reflects the latest write rather than the first.

        Only UNCLAIMED rows are reused: once a receiver has taken a hand-off, that
        row is its delivered record and a later hand-off from the same session is
        genuinely new work.

        This is the single write seam for hand-offs, so both degenerate targets are
        normalised HERE rather than at any one caller — the CLI, the orchestration
        path, and any future one all get the same treatment.

        The ``t3 worker``'s durable principal
        (:data:`~teatree.core.session_identity.LOOP_RUNNER_SESSION_ID`) is a slot
        alias no receiver can ever have, so it is PARKED (``""``) for the next
        session; four rows had accumulated addressed there, claimable by nobody. A
        target equal to the author is REFUSED
        (:class:`SelfAddressedHandoverError`): :meth:`claimable_for` admits only the
        session named by ``to_session``, and excludes the session named by
        ``from_session``, so such a row is claimable by nobody.

        Both are also DB constraints on :class:`~teatree.core.models.SessionHandover`,
        so a raw ``.create()`` cannot reintroduce either shape.
        """
        if is_loop_runner_session(to_session):
            to_session = ""
        if from_session and to_session == from_session:
            msg = (
                f"session {from_session!r} cannot hand off to itself — a self-addressed hand-off is "
                "claimable by no session (the target is excluded as its own author). "
                "Name a different target, or omit it to park the hand-off for the next session."
            )
            raise SelfAddressedHandoverError(msg)
        existing = self._unclaimed_for(from_session)
        if existing is None:
            try:
                with transaction.atomic():
                    row = self.create(from_session=from_session, to_session=to_session, payload=payload)
            except IntegrityError:
                # A concurrent create for this author won the unique constraint between
                # the lookup and the insert. A hand-off must never fail because the
                # session raced itself, so re-read and take the absorb branch instead.
                existing = self._unclaimed_for(from_session)
                if existing is None:
                    raise
            else:
                return HandoverWrite(row=row, absorbed=False, previous_bytes=0)
        return _absorb(existing, to_session=to_session, payload=payload)

    def _unclaimed_for(self, from_session: str) -> "SessionHandover | None":
        """This author's single unclaimed row, or ``None``.

        Ordered by pk so the OLDEST wins on any DB that predates the partial unique
        constraint — the choice stays deterministic rather than resting on it.
        """
        return self.filter(from_session=from_session, claimed_at__isnull=True).order_by("pk").first()

    def claimable_for(self, session_id: str) -> "SessionHandoverQuerySet":
        """Unclaimed hand-offs this session may take over.

        A hand-off is claimable by ``session_id`` when it is unclaimed
        (``claimed_at IS NULL``) AND either explicitly addressed to it
        (``to_session == session_id``) or addressed to "next session"
        (``to_session == ""``). A session never claims a hand-off it itself
        created — that would re-inject a session's own snapshot back into it.

        Those two conditions are mutually exclusive when ``from_session ==
        to_session``: the address admits only that one session, and the exclusion
        removes it, so the row is claimable by no possible ``session_id``. The
        exclusion is what this method is for and stays; the degenerate row is
        refused at creation instead (:meth:`create_handover`).
        """
        return (
            self.filter(claimed_at__isnull=True)
            .filter(Q(to_session=session_id) | Q(to_session=""))
            .exclude(from_session=session_id)
        )

    def claim_all(self, session_id: str) -> list["SessionHandover"]:
        """Atomically DRAIN every pending hand-off claimable by ``session_id`` (#3555).

        The queue is drained, not sampled. A hand-off explicitly targeted AT this
        session comes first (more specific than the open broadcast); the parked
        ``to_session == ""`` tier follows OLDEST-first, so the backlog makes
        progress instead of one newest row starving every older one forever —
        the next session to start would again find a newer row on top, so a
        claim-one policy never revisits them.

        Each claim is the same CAS (``UPDATE ... WHERE claimed_at IS NULL``), so
        a row a concurrent SessionStart hook already took matches 0 rows and is
        skipped. Returns the rows this caller won, in delivery order.
        """
        candidates = self.claimable_for(session_id).order_by("-to_session", "created_at", "id")
        now = timezone.now()
        claimed: list[SessionHandover] = []
        for pk in list(candidates.values_list("pk", flat=True)):
            won = self.filter(pk=pk, claimed_at__isnull=True).update(claimed_at=now, claimed_by=session_id)
            if won != 1:
                continue
            row = self.filter(pk=pk).first()
            if row is not None:
                claimed.append(row)
        return claimed


SessionHandoverManager = models.Manager.from_queryset(SessionHandoverQuerySet)
