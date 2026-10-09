"""Shared ticket-reference resolution lives on the manager (#694).

``visit-phase`` and ``pr create`` must accept the *same* identifier set
(pk / issue number / issue URL). Before #694 ``visit-phase`` did a pk-only
``Ticket.objects.get(pk=...)`` so passing an issue number raised
``DoesNotExist`` and silently dropped the phase.
"""

import pytest
from django.test import TestCase

from teatree.core.models import Ticket


class TestTicketResolve(TestCase):
    def test_resolves_by_pk(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        assert Ticket.objects.resolve(str(ticket.pk)).pk == ticket.pk

    def test_resolves_by_issue_url(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://github.com/souliane/teatree/issues/694")
        resolved = Ticket.objects.resolve("https://github.com/souliane/teatree/issues/694")
        assert resolved.pk == ticket.pk

    def test_resolves_by_bare_issue_number_when_no_such_pk(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://github.com/souliane/teatree/issues/694")
        # pk is small (1); 694 is not a pk -> falls back to issue_url match.
        resolved = Ticket.objects.resolve("694")
        assert resolved.pk == ticket.pk

    def test_pk_takes_precedence_over_issue_number(self) -> None:
        first = Ticket.objects.create(overlay="test", issue_url="https://github.com/souliane/teatree/issues/999")
        # Create a ticket whose issue_url ends in /<first.pk> to prove pk wins.
        Ticket.objects.create(
            overlay="test",
            issue_url=f"https://github.com/souliane/teatree/issues/{first.pk}",
        )
        assert Ticket.objects.resolve(str(first.pk)).pk == first.pk

    def test_resolves_bare_number_issue_url(self) -> None:
        # #707: issue_url stored as the bare string "694" (not a forge URL).
        # `pr create 694` must resolve, not raise DoesNotExist.
        ticket = Ticket.objects.create(overlay="test", issue_url="694")
        resolved = Ticket.objects.resolve("694")
        assert resolved.pk == ticket.pk

    def test_pk_precedence_over_bare_number_issue_url(self) -> None:
        # A ticket whose issue_url is the bare string equal to another
        # ticket's pk must not shadow the pk lookup.
        by_pk = Ticket.objects.create(overlay="test")
        Ticket.objects.create(overlay="test", issue_url=str(by_pk.pk))
        assert Ticket.objects.resolve(str(by_pk.pk)).pk == by_pk.pk

    def test_raises_does_not_exist_for_unknown_ref(self) -> None:
        with pytest.raises(Ticket.DoesNotExist):
            Ticket.objects.resolve("https://github.com/souliane/teatree/issues/12345")

    def test_raises_does_not_exist_for_unknown_number(self) -> None:
        with pytest.raises(Ticket.DoesNotExist):
            Ticket.objects.resolve("87654")

    def test_resolves_by_repo_namespaced_key(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://github.com/acme-eng/bugs/issues/2242")
        resolved = Ticket.objects.resolve("acme-eng/bugs#2242")
        assert resolved.pk == ticket.pk

    def test_repo_namespaced_key_never_collides_across_repos(self) -> None:
        """The #2293 regression.

        A bare-number lookup is ambiguous; the repo-namespaced key is
        not — passing the full repo path resolves the exact ticket even
        when another repo shares the issue number.
        """
        bugs = Ticket.objects.create(overlay="test", issue_url="https://github.com/acme-eng/bugs/issues/2242")
        Ticket.objects.create(overlay="test", issue_url="https://github.com/acme-product/repo/issues/2242")

        assert Ticket.objects.resolve("acme-eng/bugs#2242").pk == bugs.pk
        assert Ticket.objects.resolve("acme-product/repo#2242").pk != bugs.pk

    def test_raises_does_not_exist_for_unknown_repo_namespaced_key(self) -> None:
        with pytest.raises(Ticket.DoesNotExist):
            Ticket.objects.resolve("acme-eng/bugs#99999")
