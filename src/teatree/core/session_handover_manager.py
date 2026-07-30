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


class SessionHandoverQuerySet(models.QuerySet):
    def create_handover(self, *, from_session: str, to_session: str, payload: str) -> "SessionHandover":
        """Persist a new pending hand-off from ``from_session``.

        ``to_session == ""`` targets "whichever session starts next". The
        row is the source of truth; the caller mirrors ``payload`` to the
        XDG file separately.
        """
        return self.create(from_session=from_session, to_session=to_session, payload=payload)

    def claimable_for(self, session_id: str) -> "SessionHandoverQuerySet":
        """Unclaimed hand-offs this session may take over.

        A hand-off is claimable by ``session_id`` when it is unclaimed
        (``claimed_at IS NULL``) AND either explicitly addressed to it
        (``to_session == session_id``) or addressed to "next session"
        (``to_session == ""``). A session never claims a hand-off it itself
        created — that would re-inject a session's own snapshot back into it.
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
