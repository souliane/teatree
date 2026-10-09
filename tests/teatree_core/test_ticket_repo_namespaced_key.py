"""``Ticket.repo_namespaced_key`` — computed on save, collision-free (#2293).

The ``context`` CLI resolves tickets through this field (see
``tests/teatree_core/test_ticket_context_command.py`` and
``tests/teatree_core/test_ticket_resolve.py`` for the resolution-layer
coverage); this file pins the model-level computation on ``save()``.
"""

import pytest
from django.db import IntegrityError, transaction
from django.test import TestCase

from teatree.core.models import Ticket


class TestTicketRepoNamespacedKeyOnSave(TestCase):
    def test_computed_from_a_github_issue_url(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://github.com/acme-eng/widgets/issues/42")
        assert ticket.repo_namespaced_key == "acme-eng/widgets#42"

    def test_computed_from_a_gitlab_issue_url(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://gitlab.com/group/sub/project/-/issues/7")
        assert ticket.repo_namespaced_key == "group/sub/project#7"

    def test_blank_for_a_pr_shaped_issue_url(self) -> None:
        """A reviewer-role ticket keyed by a PR/MR URL gets no repo key (#2293).

        GitLab issues and merge requests are separate numbering sequences —
        deriving the same key shape from both would risk a collision, so
        this is a deliberate no-op, not an oversight.
        """
        ticket = Ticket.objects.create(
            overlay="acme",
            role=Ticket.Role.REVIEWER,
            issue_url="https://gitlab.com/group/project/-/merge_requests/7",
        )
        assert ticket.repo_namespaced_key == ""

    def test_blank_for_a_non_forge_issue_url(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/issues/1")
        assert ticket.repo_namespaced_key == ""

    def test_blank_for_a_bare_number_issue_url(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="694")
        assert ticket.repo_namespaced_key == ""

    def test_blank_when_issue_url_is_blank(self) -> None:
        ticket = Ticket.objects.create(overlay="acme")
        assert ticket.repo_namespaced_key == ""

    def test_does_not_recompute_an_already_set_key(self) -> None:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://github.com/acme-eng/widgets/issues/42",
            repo_namespaced_key="manually-set",
        )
        ticket.save()
        assert ticket.repo_namespaced_key == "manually-set"

    def test_two_repos_sharing_an_issue_number_get_distinct_keys(self) -> None:
        first = Ticket.objects.create(overlay="acme", issue_url="https://github.com/acme-eng/bugs/issues/2242")
        second = Ticket.objects.create(overlay="acme", issue_url="https://github.com/acme-product/repo/issues/2242")
        assert first.repo_namespaced_key != second.repo_namespaced_key


class TestTicketRepoNamespacedKeyUniqueConstraint(TestCase):
    def test_duplicate_key_is_rejected_at_the_db_level(self) -> None:
        Ticket.objects.create(overlay="acme", issue_url="https://github.com/acme-eng/widgets/issues/42")
        with pytest.raises(IntegrityError), transaction.atomic():
            Ticket.objects.create(overlay="acme", repo_namespaced_key="acme-eng/widgets#42")

    def test_two_blank_keys_do_not_collide(self) -> None:
        Ticket.objects.create(overlay="acme", issue_url="")
        Ticket.objects.create(overlay="acme", issue_url="")
