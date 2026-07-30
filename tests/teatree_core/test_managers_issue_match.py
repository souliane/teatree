"""Tests for teatree.core.managers_issue_match — issue-URL alias matching."""

from django.test import TestCase

from teatree.core.managers_issue_match import matching_issue_q
from teatree.core.models import Ticket


class MatchingIssueQTests(TestCase):
    def test_matches_a_sibling_url_alias_via_the_namespaced_key(self) -> None:
        """A ticket stored under the work_items alias is found by the /-/issues/ alias."""
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/acme-org/backend/-/work_items/1701",
        )
        alias = "https://gitlab.com/acme-org/backend/-/issues/1701"
        # the raw string differs, so an issue_url-only lookup would miss it
        assert not Ticket.objects.filter(issue_url=alias).exists()
        # matching_issue_q folds the aliases onto the same repo_namespaced_key
        found = Ticket.objects.filter(matching_issue_q(alias)).get()
        assert found.pk == ticket.pk

    def test_falls_back_to_exact_issue_url_when_the_key_is_blank(self) -> None:
        """A non-forge issue_url has no namespaced key, so it matches on the raw string only."""
        ticket = Ticket.objects.create(overlay="test", issue_url="auto:1701-some-branch")
        found = Ticket.objects.filter(matching_issue_q("auto:1701-some-branch")).get()
        assert found.pk == ticket.pk
        assert not Ticket.objects.filter(matching_issue_q("auto:other-branch")).exists()
