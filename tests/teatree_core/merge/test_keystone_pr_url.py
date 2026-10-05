"""The merge keystone judges the repo of the PR it is merging by that PR's recorded URL, never a guess.

The keystone holds only the slug and number. A recorded ``PullRequest`` row (or the ticket's recorded
URLs) names the host the repo's visibility is asked under. Two GitLab hosts can carry the same group
path and MR number: records on both name neither, so the keystone fails closed rather than judging the
repo by whichever record came first.
"""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.merge.authorization import assert_merge_provenance_trusted
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.models import PullRequest, Ticket
from teatree.core.review import author_trust

_OURS = "https://gitlab.example.test/acme/widget/-/merge_requests/7"
_OTHER = "https://gitlab.com/acme/widget/-/merge_requests/7"
_QUALIFIED = "gitlab.example.test/acme/widget"
_QUERY = "teatree.core.merge.ci_rollup.CodeHostQuery"


@contextmanager
def _colleague_on_a_repo_private_under_its_host() -> Iterator[None]:
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "teatree.hooks._private_repo_entries.private_repo_visibility",
                side_effect=lambda slug, *_args, **_kwargs: "PRIVATE" if slug == _QUALIFIED else None,
            )
        )
        stack.enter_context(patch.object(author_trust, "trusted_handles", return_value=set()))
        stack.enter_context(patch(f"{_QUERY}.pr_author", return_value="colleague"))
        stack.enter_context(patch(f"{_QUERY}.pr_same_repo", return_value=True))
        yield


def _record(*urls: str) -> None:
    ticket = Ticket.objects.create(overlay="t3-teatree")
    for url in urls:
        PullRequest.objects.create(ticket=ticket, url=url, repo="acme/widget", iid="7")


class TestTheKeystoneNeverGuessesTheHost(TestCase):
    def test_a_record_on_one_host_names_it(self) -> None:
        _record(_OURS)

        with _colleague_on_a_repo_private_under_its_host():
            assert assert_merge_provenance_trusted(slug="acme/widget", pr_id=7, host_kind="gitlab") is None

    def test_records_on_two_hosts_fail_closed_whichever_was_recorded_first(self) -> None:
        for order in ((_OURS, _OTHER), (_OTHER, _OURS)):
            PullRequest.objects.all().delete()
            _record(*order)

            with (
                self.subTest(first=order[0]),
                _colleague_on_a_repo_private_under_its_host(),
                pytest.raises(MergePreconditionError, match="not trusted to auto-merge"),
            ):
                assert_merge_provenance_trusted(slug="acme/widget", pr_id=7, host_kind="gitlab")
