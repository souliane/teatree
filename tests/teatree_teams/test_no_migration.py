"""No schema change for the team claim-namespace (#1838 PR#6).

``Task.claimed_by`` is already a free-form ``CharField`` — a teammate is just
another ``claimed_by`` value (``team:<role>``). PR#6 adds NO model field and NO
migration. This pins both facts: the field round-trips a ``team:<role>`` key
through the existing CAS claim path, and ``makemigrations --check`` (run in the
gate) stays clean.
"""

import uuid

import pytest

from teatree.core.models import Session, Task, Ticket
from teatree.teams.roles import TeamRole, team_claim_slot


def _pending_task() -> Task:
    ticket = Ticket.objects.create(overlay="", issue_url=f"https://example.com/issues/{uuid.uuid4().hex}")
    session = Session.objects.create(ticket=ticket, agent_id="a")
    return Task.objects.create(ticket=ticket, session=session, status=Task.Status.PENDING)


@pytest.mark.django_db
class TestClaimedByAcceptsTeamSlot:
    def test_claim_next_pending_accepts_a_team_role_slot(self) -> None:
        _pending_task()
        slot = team_claim_slot(TeamRole.CORE_MAKER)
        claimed = Task.objects.claim_next_pending(claimed_by=slot)
        assert claimed is not None
        assert claimed.claimed_by == slot

    def test_claimed_by_round_trips_every_team_role(self) -> None:
        for role in TeamRole:
            slot = team_claim_slot(role)
            task = _pending_task()
            task.claimed_by = slot
            task.save(update_fields=["claimed_by"])
            task.refresh_from_db()
            assert task.claimed_by == slot
