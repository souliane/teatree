"""The sweep's CLEAR lookup, and what it reports when it finds no usable one (#4249).

A ``MergeClear`` may be issued with a head BRANCH in ``slug`` — nothing rejects it,
because ``_looks_like_owner_repo`` reads ``review-fixes/docs`` as ``owner/repo``. The
merge path recovers from that; the exact ``slug=`` join the sweep used did not, so the
authorisation stayed actionable and invisible for as long as it existed.
"""

from typing import ClassVar, cast
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models.merge_clear import MergeClear
from teatree.loop.scanners.pr_sweep_clear_lookup import clear_scopes_to_repo, look_up_clear_for_head
from tests.factories import MergeClearFactory, TicketFactory

_HEAD = "5e3dd9fd" + "0" * 32
_OTHER_HEAD = "a1b2c3d4" + "0" * 32
_REPO = "souliane/teatree"
_REGISTRY = "teatree.core.merge.pr_slug_resolution._iter_candidate_repo_slugs"
_ORIGIN = "teatree.core.merge.pr_slug_resolution._project_repo_slug"


class _RegistryCase(TestCase):
    """Pins the registry enumeration and the clone origin — both machine-dependent."""

    registered: ClassVar[list[str]] = [_REPO]

    def setUp(self) -> None:
        super().setUp()
        for target, value in ((_REGISTRY, list(self.registered)), (_ORIGIN, _REPO)):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def clear(slug: str, *, issue_url: str = "", head: str = _HEAD, pr_id: int = 4230) -> MergeClear:
        ticket = TicketFactory(issue_url=issue_url) if issue_url else None
        return cast("MergeClear", MergeClearFactory(slug=slug, pr_id=pr_id, reviewed_sha=head, ticket=ticket))


class TestBranchShapedSlugIsFound(_RegistryCase):
    def test_a_clear_slugged_with_its_head_branch_is_found_via_its_ticket(self) -> None:
        clear = self.clear("review-fixes/docs", issue_url=f"https://github.com/{_REPO}/issues/4230")

        found = look_up_clear_for_head(slug=_REPO, pr_id=4230, head_sha=_HEAD)

        assert found.clear is not None
        assert found.clear.pk == clear.pk

    def test_a_clear_slugged_with_its_head_branch_is_found_via_the_clone_origin(self) -> None:
        clear = self.clear("review-fixes/docs")

        found = look_up_clear_for_head(slug=_REPO, pr_id=4230, head_sha=_HEAD)

        assert found.clear is not None
        assert found.clear.pk == clear.pk

    def test_a_workstream_slug_is_found_too(self) -> None:
        clear = self.clear("statusline-stale-wakeup", issue_url=f"https://github.com/{_REPO}/issues/4230")

        found = look_up_clear_for_head(slug=_REPO, pr_id=4230, head_sha=_HEAD)

        assert found.clear is not None
        assert found.clear.pk == clear.pk

    def test_an_exact_repo_slug_still_matches(self) -> None:
        clear = self.clear(_REPO)

        found = look_up_clear_for_head(slug=_REPO, pr_id=4230, head_sha=_HEAD)

        assert found.clear is not None
        assert found.clear.pk == clear.pk


class TestRepoScopingIsPreserved(_RegistryCase):
    registered: ClassVar[list[str]] = [_REPO, "downstream-org/app"]

    def test_a_clear_for_another_registered_repo_never_matches(self) -> None:
        self.clear("downstream-org/app")

        found = look_up_clear_for_head(slug=_REPO, pr_id=4230, head_sha=_HEAD)

        assert found.clear is None
        assert found.unusable is None

    def test_a_clear_whose_ticket_names_another_repo_never_matches(self) -> None:
        self.clear("review-fixes/docs", issue_url="https://github.com/downstream-org/app/issues/4230")

        found = look_up_clear_for_head(slug=_REPO, pr_id=4230, head_sha=_HEAD)

        assert found.clear is None
        assert found.unusable is None

    def test_a_consumed_clear_never_matches(self) -> None:
        clear = self.clear("review-fixes/docs")
        clear.consumed_at = timezone.now()
        clear.save(update_fields=["consumed_at"])

        found = look_up_clear_for_head(slug=_REPO, pr_id=4230, head_sha=_HEAD)

        assert found.clear is None
        assert found.unusable is None

    def test_a_clear_for_another_pr_never_matches(self) -> None:
        self.clear("review-fixes/docs", pr_id=4231)

        assert look_up_clear_for_head(slug=_REPO, pr_id=4230, head_sha=_HEAD).clear is None

    def test_an_unresolvable_sweep_slug_scopes_to_nothing(self) -> None:
        assert clear_scopes_to_repo(self.clear(_REPO), slug="") is False


class TestPresentButUnusableIsReportedDistinctly(_RegistryCase):
    def test_a_stale_sha_clear_comes_back_as_unusable_not_as_nothing(self) -> None:
        clear = self.clear("review-fixes/docs", head=_OTHER_HEAD)

        found = look_up_clear_for_head(slug=_REPO, pr_id=4230, head_sha=_HEAD)

        assert found.clear is None
        assert found.unusable is not None
        assert found.unusable.pk == clear.pk

    def test_no_clear_at_all_reports_neither(self) -> None:
        found = look_up_clear_for_head(slug=_REPO, pr_id=4230, head_sha=_HEAD)

        assert found.clear is None
        assert found.unusable is None

    def test_a_usable_clear_beside_a_stale_one_still_wins(self) -> None:
        self.clear("review-fixes/docs", head=_OTHER_HEAD)
        usable = self.clear(_REPO)

        found = look_up_clear_for_head(slug=_REPO, pr_id=4230, head_sha=_HEAD)

        assert found.clear is not None
        assert found.clear.pk == usable.pk
