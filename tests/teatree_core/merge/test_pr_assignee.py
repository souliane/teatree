"""Tests for the #3100 assignee resolver (mirrors ``pr_assignee``).

The resolver consults the trusted-identities registry (#1773) for the
sanctioned forge handle first, falls back to the host-token login, and
returns the first candidate that is actually assignable on the repo —
degrading to an unassigned PR (``""``) rather than a failed create.
"""

from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
from django.db import OperationalError, ProgrammingError
from django.test import TestCase

from teatree.core.merge.pr_assignee import resolve_pr_assignee
from teatree.core.models import TrustedIdentity

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@dataclass
class StubHost:
    login: str
    assignable_logins: frozenset[str] = frozenset()
    probed: list[tuple[str, str]] = field(default_factory=list)

    def current_user(self) -> str:
        return self.login

    def is_assignable(self, *, repo: str, login: str) -> bool:
        self.probed.append((repo, login))
        return login in self.assignable_logins


class TestResolvePrAssignee(TestCase):
    def setUp(self) -> None:
        TrustedIdentity.objects.all().delete()

    def test_keeps_assignable_token_login(self) -> None:
        host = StubHost(login="souliane", assignable_logins=frozenset({"souliane"}))

        assert resolve_pr_assignee(host, repo="souliane/teatree") == "souliane"

    def test_drops_non_assignable_token_login(self) -> None:
        host = StubHost(login="pullonly-bot", assignable_logins=frozenset())

        assert resolve_pr_assignee(host, repo="souliane/teatree") == ""

    def test_prefers_registry_handle_over_non_assignable_token_login(self) -> None:
        # The reported bug: the token PAT is pull-only so its login is not
        # assignable, but the registry's sanctioned handle IS — the PR must be
        # assigned to it rather than left unassigned or failing the create.
        TrustedIdentity.objects.create(platform="github", handle="souliane")
        host = StubHost(login="pullonly-bot", assignable_logins=frozenset({"souliane"}))

        assert resolve_pr_assignee(host, repo="souliane/teatree") == "souliane"
        assert ("souliane/teatree", "souliane") in host.probed

    def test_registry_handle_tried_before_token_login(self) -> None:
        TrustedIdentity.objects.create(platform="github", handle="souliane")
        host = StubHost(login="pullonly-bot", assignable_logins=frozenset({"souliane", "pullonly-bot"}))

        assert resolve_pr_assignee(host, repo="souliane/teatree") == "souliane"
        assert host.probed[0] == ("souliane/teatree", "souliane")

    def test_empty_when_no_candidate_is_assignable(self) -> None:
        TrustedIdentity.objects.create(platform="github", handle="souliane")
        host = StubHost(login="pullonly-bot", assignable_logins=frozenset())

        assert resolve_pr_assignee(host, repo="souliane/teatree") == ""

    def test_token_login_deduped_against_registry_handle(self) -> None:
        TrustedIdentity.objects.create(platform="github", handle="souliane")
        host = StubHost(login="souliane", assignable_logins=frozenset())

        assert resolve_pr_assignee(host, repo="souliane/teatree") == ""
        assert host.probed == [("souliane/teatree", "souliane")]

    def test_no_probe_when_no_identity_at_all(self) -> None:
        host = StubHost(login="", assignable_logins=frozenset())

        assert resolve_pr_assignee(host, repo="souliane/teatree") == ""
        assert host.probed == []


class TestRegistryUnavailableFallsBackToTokenLogin(TestCase):
    def test_db_error_falls_back_to_token_login(self) -> None:
        host = StubHost(login="souliane", assignable_logins=frozenset({"souliane"}))
        for exc in (
            OperationalError("no such table: teatree_trusted_identity"),
            ProgrammingError("relation does not exist"),
            RuntimeError("canonical DB not reachable"),
        ):
            with (
                self.subTest(exc=type(exc).__name__),
                patch.object(TrustedIdentity.objects, "ordered_handles", side_effect=exc),
            ):
                assert resolve_pr_assignee(host, repo="souliane/teatree") == "souliane"
