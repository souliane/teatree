"""Factory Boy factories for teatree core models used in DB-backed tests.

These build rows directly via the ORM (``DjangoModelFactory`` ⇒
``objects.create``), deliberately bypassing guarded model factories like
``MergeClear.issue()`` so a test can construct an *invalid* row and prove the
merge-time gate refuses it independently of the issue-time gate.
"""

import factory
from factory.django import DjangoModelFactory

from teatree.core.models import EvalRunRecord, ImplementedIssueMarker, MergeClear, Ticket

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


class ImplementedIssueMarkerFactory(DjangoModelFactory[ImplementedIssueMarker]):
    class Meta:
        model = ImplementedIssueMarker

    issue_url = factory.Sequence(lambda n: f"https://github.com/souliane/teatree/issues/{n}")
    overlay = "t3-teatree"
    state = ImplementedIssueMarker.State.DISPATCHED
    head_sha = _FORTY_HEX

    class Params:
        ticket_created = factory.Trait(state=ImplementedIssueMarker.State.TICKET_CREATED)
        abandoned = factory.Trait(state=ImplementedIssueMarker.State.ABANDONED)


class EvalRunRecordFactory(DjangoModelFactory[EvalRunRecord]):
    class Meta:
        model = EvalRunRecord

    run_id = factory.Sequence(lambda n: f"run{n:032x}")
    scenario = factory.Sequence(lambda n: f"scenario_{n}")
    model = "haiku"
    passed = True
    score = 1.0
    trials = 1
    git_sha = _FORTY_HEX

    class Params:
        failing = factory.Trait(passed=False, score=0.0)
        was_skipped = factory.Trait(skipped=True, passed=False, score=0.0)
