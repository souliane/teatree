"""The keystone merge-provenance gate (BLUEPRINT §17.4.3 step 6 / #3244).

The load-bearing deny: every sanctioned merge funnels through
:func:`merge_ticket_pr`, so a fork / cross-repo PR — or an untrusted author on a
PUBLIC repo when provenance is unreported — can never auto-merge even if a
scanner forgets the check. These tests pin the must-ALLOW / must-DENY matrix at
the keystone with the real models and FSM — only the ``gh`` subprocess (via
``gh_runner``) and the repo-visibility resolver are stubbed.

Anti-vacuity: drop ``assert_merge_provenance_trusted`` from
``_merge_ticket_pr_inner`` and the untrusted / fork tests below go green-merge
instead of raising — the gate is what makes them pass.
"""

import json
from contextlib import AbstractContextManager
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.merge import execution
from teatree.core.merge.authorization import assert_merge_provenance_trusted
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.merge.execution import execute_bound_merge, merge_ticket_pr
from teatree.core.models import MergeClear, Ticket, TrustedIdentity
from teatree.core.review import author_trust
from teatree.utils.pr_ref import PrRef
from tests.teatree_core.conftest import CommandOverlay, seed_merge_safe_verdict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "a" * 40
_GREEN = '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]'
_MOCK_OVERLAY = {"t3-teatree": CommandOverlay()}


def _seed_known() -> None:
    TrustedIdentity.objects.get_or_create(platform="github", handle="souliane")
    TrustedIdentity.objects.get_or_create(platform="github", handle="trusted-bot")
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

    def __init__(self, *, author: str = "souliane", head: str = _SHA, cross_repo: bool | None = None) -> None:
        self.author = author
        self.head = head
        # None ⇒ the forge does not report provenance (empty ``isCrossRepository``)
        # so the gate falls back to the author check; True/False drive the fork gate.
        self.cross_repo = cross_repo
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        joined = " ".join(argv)
        responses = {
            ".author.login": self.author,
            ".isCrossRepository": "" if self.cross_repo is None else str(self.cross_repo).lower(),
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
    # Seed the #2829 sibling verdict (the author gate runs before the merge-verdict
    # gate, so a refuse-on-untrusted-author test is unaffected by this seed).
    seed_merge_safe_verdict(slug=clear.slug, pr_id=clear.pr_id, sha=clear.reviewed_sha)
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
        stub = _GhStub(author="trusted-bot")
        _run(_clear(ticket), stub, internal=False)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED


class TestPublicRepoMustDeny(TestCase):
    def setUp(self) -> None:
        _seed_known()

    def test_external_author_refused_and_never_merges(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        stub = _GhStub(author="evilhacker")
        with pytest.raises(MergePreconditionError, match="not trusted to auto-merge"):
            _run(_clear(ticket), stub, internal=False)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert stub.attempted_merge is False

    def test_unknown_empty_author_refused_fail_closed(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        stub = _GhStub(author="")
        with pytest.raises(MergePreconditionError, match="not trusted to auto-merge"):
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


class TestForkAlwaysHoldsAtKeystone(TestCase):
    """A FORK PR authored by the TRUSTED operator still holds — the strict model.

    This is the hardest pin: on today's pure author gate a souliane-authored PR
    merges; the provenance gate refuses it when the head branch lives in a fork.
    """

    def setUp(self) -> None:
        _seed_known()

    def test_trusted_author_fork_refused_and_never_merges(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        stub = _GhStub(author="souliane", cross_repo=True)
        with pytest.raises(MergePreconditionError, match="fork / cross-repo"):
            _run(_clear(ticket), stub, internal=False)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert stub.attempted_merge is False

    def test_trusted_author_same_repo_merges(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        stub = _GhStub(author="souliane", cross_repo=False)
        _run(_clear(ticket), stub, internal=False)
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert stub.attempted_merge is True


class TestProvenanceGateUnit(TestCase):
    def setUp(self) -> None:
        _seed_known()

    def _patches(self, *, author: str, same_repo: bool | None) -> tuple[AbstractContextManager[object], ...]:
        return (
            patch("teatree.core.merge.ci_rollup.CodeHostQuery.pr_author", return_value=author),
            patch("teatree.core.merge.ci_rollup.CodeHostQuery.pr_same_repo", return_value=same_repo),
            patch.object(author_trust, "repo_is_internal", return_value=False),
        )

    def test_fork_holds_even_for_trusted_author(self) -> None:
        p1, p2, p3 = self._patches(author="souliane", same_repo=False)
        with p1, p2, p3, pytest.raises(MergePreconditionError, match="fork / cross-repo"):
            assert_merge_provenance_trusted(slug="souliane/teatree", pr_id=1)

    def test_same_repo_passes_even_for_unlisted_bot(self) -> None:
        p1, p2, p3 = self._patches(author="app/github-actions", same_repo=True)
        with p1, p2, p3:
            assert_merge_provenance_trusted(slug="souliane/teatree", pr_id=1)

    def test_unknown_provenance_falls_back_and_denies_untrusted(self) -> None:
        p1, p2, p3 = self._patches(author="evilhacker", same_repo=None)
        with p1, p2, p3, pytest.raises(MergePreconditionError, match="not trusted to auto-merge"):
            assert_merge_provenance_trusted(slug="souliane/teatree", pr_id=1)

    def test_unknown_provenance_falls_back_and_allows_trusted(self) -> None:
        p1, p2, p3 = self._patches(author="souliane", same_repo=None)
        with p1, p2, p3:
            assert_merge_provenance_trusted(slug="souliane/teatree", pr_id=1)

    def test_fetch_failure_yields_none_then_denies_untrusted_author(self) -> None:
        def _fail(_argv: list[str]) -> tuple[int, str, str]:
            return (1, "", "boom")

        with (
            patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=_fail),
            patch.object(author_trust, "repo_is_internal", return_value=False),
            pytest.raises(MergePreconditionError, match="not trusted to auto-merge"),
        ):
            assert_merge_provenance_trusted(slug="souliane/teatree", pr_id=1)


class TestBypassPathForkRefused(TestCase):
    """The solo bypass (``merge_pr_squash_bound`` → ``execute_bound_merge``) also holds a fork.

    ``execute_bound_merge`` is the shared chokepoint BOTH merge paths cross. The
    provenance gate fires HERE too (defence-in-depth), so a fork PR cannot slip
    through the bypass even with NO keystone preconditions run.
    """

    def setUp(self) -> None:
        _seed_known()

    def test_fork_ref_refused_before_any_merge(self) -> None:
        with (
            patch("teatree.core.merge.ci_rollup.CodeHostQuery.pr_author", return_value="souliane"),
            patch("teatree.core.merge.ci_rollup.CodeHostQuery.pr_same_repo", return_value=False),
            patch.object(author_trust, "repo_is_internal", return_value=False),
            pytest.raises(MergePreconditionError, match="fork / cross-repo"),
        ):
            execute_bound_merge(ref=PrRef(slug="souliane/teatree", pr_id=1), expected_head_oid=_SHA)
