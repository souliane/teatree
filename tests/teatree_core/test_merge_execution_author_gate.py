"""The keystone PUBLIC-repo author-trust gate (BLUEPRINT §17.4.3 step 6 / #1773).

The load-bearing deny: every sanctioned merge funnels through
:func:`merge_ticket_pr`, so an untrusted author on a PUBLIC repo can never
auto-merge even if a scanner forgets the author. These tests pin the
must-ALLOW / must-DENY matrix at the keystone with the real models and FSM —
only the ``gh`` subprocess (via ``gh_runner``) and the repo-visibility resolver
are stubbed.

Anti-vacuity: drop ``assert_public_repo_author_trusted`` from
``_merge_ticket_pr_inner`` and the untrusted-author tests below go green-merge
instead of raising — the gate is what makes them pass.
"""

import json
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core import author_trust
from teatree.core.merge import authorization, execution
from teatree.core.merge.authorization import assert_public_repo_author_trusted
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.merge.execution import merge_ticket_pr
from teatree.core.models import MergeClear, Ticket, TrustedIdentity
from tests.teatree_core.conftest import CommandOverlay

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "a" * 40
_GREEN = '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]'
_MOCK_OVERLAY = {"t3-teatree": CommandOverlay()}


def _seed_known() -> None:
    TrustedIdentity.objects.get_or_create(platform="github", handle="souliane")
    TrustedIdentity.objects.get_or_create(platform="github", handle="adrien-oper")
    TrustedIdentity.objects.get_or_create(platform="gitlab", handle="adrien.cossa")


def _clear(ticket: Ticket, **overrides: object) -> MergeClear:
    defaults: dict[str, object] = {
        "ticket": ticket,
        "pr_id": 859,
        "slug": "souliane/teatree",
        "reviewed_sha": _SHA,
        "reviewer_identity": "cold-reviewer",
        "gh_verify_result": MergeClear.VerifyResult.GREEN,
        "blast_class": MergeClear.BlastClass.DOCS,
    }
    defaults.update(overrides)
    return MergeClear.objects.create(**defaults)


class _GhStub:
    """Scripted ``gh`` responses; ``author`` drives the §17.4.3 step-6 author gate."""

    def __init__(self, *, author: str = "souliane", head: str = _SHA) -> None:
        self.author = author
        self.head = head
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        joined = " ".join(argv)
        responses = {
            ".author.login": self.author,
            "baseRefName": "main",
            "required_status_checks": json.dumps({"contexts": []}),
            "headRefOid": self.head,
            "isDraft": "false",
            "statusCheckRollup": _GREEN,
        }
        for needle, out in responses.items():
            if needle in joined:
                return (0, out, "")
        if "pulls" in joined and "merge" in joined:
            return (0, '{"sha": "merged0deadbeef"}', "")
        return (0, "", "")

    @property
    def attempted_merge(self) -> bool:
        return any("pulls" in " ".join(c) and "merge" in " ".join(c) for c in self.calls)


def _run(clear: MergeClear, stub: _GhStub, *, internal: bool) -> execution.MergeOutcome:
    with (
        patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=stub),
        patch.object(author_trust, "repo_is_internal", return_value=internal),
    ):
        return merge_ticket_pr(clear=clear, executing_loop_identity="merge-loop")


class TestPublicRepoMustAllow(TestCase):
    def setUp(self) -> None:
        _seed_known()

    def test_seeded_github_author_merges(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        stub = _GhStub(author="souliane")
        outcome = _run(_clear(ticket), stub, internal=False)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert stub.attempted_merge is True
        assert outcome.merged_sha

    def test_second_github_login_merges(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        stub = _GhStub(author="adrien-oper")
        _run(_clear(ticket), stub, internal=False)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED


class TestPublicRepoMustDeny(TestCase):
    def setUp(self) -> None:
        _seed_known()

    def test_external_author_refused_and_never_merges(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        stub = _GhStub(author="evilhacker")
        with pytest.raises(MergePreconditionError, match="not a trusted identity"):
            _run(_clear(ticket), stub, internal=False)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert stub.attempted_merge is False

    def test_unknown_empty_author_refused_fail_closed(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        stub = _GhStub(author="")
        with pytest.raises(MergePreconditionError, match="not a trusted identity"):
            _run(_clear(ticket), stub, internal=False)
        assert stub.attempted_merge is False


class TestPrivateRepoSkipsAuthorCheck(TestCase):
    def test_external_author_merges_on_private_repo(self) -> None:
        # No trust seeding: a private/internal repo must skip the author check
        # entirely (the user owns access control).
        TrustedIdentity.objects.all().delete()
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        stub = _GhStub(author="evilhacker")
        _run(_clear(ticket), stub, internal=True)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert stub.attempted_merge is True


class TestAuthorGateUnit(TestCase):
    def setUp(self) -> None:
        _seed_known()

    def test_assert_public_repo_author_trusted_raises_on_untrusted(self) -> None:
        with (
            patch.object(authorization, "fetch_pr_author", return_value="evilhacker"),
            patch.object(author_trust, "repo_is_internal", return_value=False),
            pytest.raises(MergePreconditionError, match="PUBLIC repo"),
        ):
            assert_public_repo_author_trusted(slug="souliane/teatree", pr_id=1)

    def test_assert_public_repo_author_trusted_passes_on_trusted(self) -> None:
        with (
            patch.object(authorization, "fetch_pr_author", return_value="souliane"),
            patch.object(author_trust, "repo_is_internal", return_value=False),
        ):
            assert_public_repo_author_trusted(slug="souliane/teatree", pr_id=1)

    def test_fetch_pr_author_failure_yields_empty_then_denies(self) -> None:
        def _fail(_argv: list[str]) -> tuple[int, str, str]:
            return (1, "", "boom")

        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_fail),
            patch.object(author_trust, "repo_is_internal", return_value=False),
            pytest.raises(MergePreconditionError, match="not a trusted identity"),
        ):
            assert_public_repo_author_trusted(slug="souliane/teatree", pr_id=1)
