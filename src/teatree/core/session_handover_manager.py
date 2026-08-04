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

from typing import TYPE_CHECKING

from django.db import models
from django.db.models import Q
from django.utils import timezone

if TYPE_CHECKING:
    from teatree.core.models.session_handover import SessionHandover


class SelfAddressedHandoverError(ValueError):
    """A hand-off addressed to the session creating it, which nothing could ever claim.

    Raised by :meth:`SessionHandoverQuerySet.create_handover` so the degenerate row
    is refused where an operator can still react, rather than persisting as a row
    that counts as pending forever. See :meth:`~SessionHandoverQuerySet.claimable_for`
    for why such a row is unreachable.
    """


class SessionHandoverQuerySet(models.QuerySet):
    def create_handover(self, *, from_session: str, to_session: str, payload: str) -> "SessionHandover":
        """Persist the pending hand-off from ``from_session``, one row per session.

        ``to_session == ""`` targets "whichever session starts next". The
        row is the source of truth; the caller mirrors ``payload`` to the
        XDG file separately.

        A session gets ONE unclaimed row, UPDATED in place. Repeated ``create``
        calls used to insert siblings, so a session that learned something after
        its first hand-off left the receiver several rows to reconcile — one
        session produced three in fourteen minutes, each superseding the last in
        prose ("Supersedes the previous addendum. Read all three."). The receiver
        cannot diff prose, so the newest row is not reliably the whole story.
        Updating keeps the latest payload authoritative and the queue one-deep per
        author. ``created_at`` is refreshed so the parked tier's oldest-first
        delivery order reflects the latest write rather than the first.

        Only UNCLAIMED rows are reused: once a receiver has taken a hand-off, that
        row is its delivered record and a later hand-off from the same session is
        genuinely new work.

        A hand-off addressed to its own author is REFUSED
        (:class:`SelfAddressedHandoverError`): :meth:`claimable_for` admits only the
        session named by ``to_session``, and excludes the session named by
        ``from_session``, so a row where the two are equal is claimable by nobody.
        This is the single write seam for hand-offs, so the refusal holds for every
        caller — the CLI, the orchestration path, and any future one.
        """
        if from_session and to_session == from_session:
            msg = (
                f"session {from_session!r} cannot hand off to itself — a self-addressed hand-off is "
                "claimable by no session (the target is excluded as its own author). "
                "Name a different target, or omit it to park the hand-off for the next session."
            )
            raise SelfAddressedHandoverError(msg)
        existing = self.filter(from_session=from_session, claimed_at__isnull=True).order_by("pk").first()
        if existing is None:
            return self.create(from_session=from_session, to_session=to_session, payload=payload)
        existing.to_session = to_session
        existing.payload = payload
        existing.created_at = timezone.now()
        existing.save(update_fields=["to_session", "payload", "created_at"])
        return existing

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
