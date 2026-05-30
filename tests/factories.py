"""Factory Boy factories for teatree core models used in DB-backed tests.

These build rows directly via the ORM (``DjangoModelFactory`` ⇒
``objects.create``), deliberately bypassing guarded model factories like
``MergeClear.issue()`` so a test can construct an *invalid* row and prove the
merge-time gate refuses it independently of the issue-time gate.
"""

import factory
from factory.django import DjangoModelFactory

from teatree.core.models import MergeClear, Ticket

_FORTY_HEX = "c" * 40


class TicketFactory(DjangoModelFactory[Ticket]):
    class Meta:
        model = Ticket

    overlay = "t3-teatree"
    state = Ticket.State.IN_REVIEW


class MergeClearFactory(DjangoModelFactory[MergeClear]):
    class Meta:
        model = MergeClear

    ticket = factory.SubFactory(TicketFactory)
    pr_id = factory.Sequence(lambda n: 900 + n)
    slug = "souliane/teatree"
    reviewed_sha = _FORTY_HEX
    reviewer_identity = "cold-reviewer"
    gh_verify_result = MergeClear.VerifyResult.GREEN
    blast_class = MergeClear.BlastClass.LOGIC

    class Params:
        pending = factory.Trait(gh_verify_result=MergeClear.VerifyResult.PENDING)
        failed = factory.Trait(gh_verify_result=MergeClear.VerifyResult.FAILED)
        substrate = factory.Trait(blast_class=MergeClear.BlastClass.SUBSTRATE)
        docs = factory.Trait(blast_class=MergeClear.BlastClass.DOCS)
