"""Rule 5 of #162: only the owner's and the factory bot's own tickets may be changed.

The whole hygiene facade hangs off ONE question — "did we file this?" — and the
failure that matters is answering *yes* for a colleague's ticket. So the
must-DENY direction is over-represented here on purpose: an unknown author, an
author-less payload, an unreadable issue and a *trusted colleague* all fail
closed. ``trusted_issue_authors`` deliberately includes colleagues, so a test
pins that it never widens the self-set.
"""

from contextlib import AbstractContextManager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.self_forge_identities import (
    ExternalIssueRefusedError,
    declared_identities_for_url,
    issue_author_is_self,
    issue_author_login,
    require_self_authored_issue,
    self_identity_set,
)
from teatree.hooks.foreign_mr_cli import declared_self_identities

_GITLAB_ISSUE = "https://gitlab.com/acme/widgets/-/issues/7"
_GITHUB_ISSUE = "https://github.com/acme/widgets/issues/7"


class _FakeHost:
    """A code host that answers ``get_issue`` from a canned payload."""

    def __init__(self, payload: object, *, current_user: str = "") -> None:
        self.payload = payload
        self._current_user = current_user
        self.calls: list[str] = []

    def get_issue(self, issue_url: str) -> object:
        self.calls.append(issue_url)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def current_user(self) -> str:
        return self._current_user


def _gitlab_payload(username: str) -> dict[str, object]:
    return {"web_url": _GITLAB_ISSUE, "author": {"username": username}}


def _github_payload(login: str) -> dict[str, object]:
    return {"html_url": _GITHUB_ISSUE, "user": {"login": login}}


class TestIssueAuthorLogin(TestCase):
    def test_reads_both_forge_shapes(self) -> None:
        assert issue_author_login(_gitlab_payload("adrien.cossa")) == "adrien.cossa"
        assert issue_author_login(_github_payload("souliane")) == "souliane"

    def test_missing_or_malformed_author_is_empty(self) -> None:
        for payload in ({}, {"author": None}, {"author": {}}, {"user": {"login": ""}}, {"author": "adrien"}):
            with self.subTest(payload=payload):
                assert issue_author_login(payload) == ""


class TestSelfIdentitySet(TestCase):
    def test_unions_aliases_declared_bots_and_current_user(self) -> None:
        host = _FakeHost({}, current_user="factory-bot")
        with (
            patch("teatree.core.self_forge_identities._configured_aliases", return_value=("Adrien.Cossa",)),
            patch("teatree.core.self_forge_identities.declared_identities_for_url", return_value=("Declared-Bot",)),
        ):
            resolved = self_identity_set(_GITLAB_ISSUE, host=host)
        assert resolved == {"adrien.cossa", "declared-bot", "factory-bot"}

    def test_a_host_that_cannot_name_itself_narrows_but_never_raises(self) -> None:
        class _Broken(_FakeHost):
            def current_user(self) -> str:
                msg = "no token"
                raise RuntimeError(msg)

        with (
            patch("teatree.core.self_forge_identities._configured_aliases", return_value=("adrien.cossa",)),
            patch("teatree.core.self_forge_identities.declared_identities_for_url", return_value=()),
        ):
            assert self_identity_set(_GITLAB_ISSUE, host=_Broken({})) == {"adrien.cossa"}

    def test_trusted_issue_authors_never_widens_the_self_set(self) -> None:
        """A trusted colleague is trusted to REVIEW, never to have their ticket edited.

        Exercises the real ``_configured_aliases`` against a settings object that
        carries BOTH lists, so wiring ``trusted_issue_authors`` into the self set —
        the way ``effective_trusted_issue_authors`` unions them for issue intake —
        turns this red.
        """
        settings = SimpleNamespace(
            user_identity_aliases=("adrien.cossa",),
            trusted_issue_authors=("colleague",),
        )
        with (
            patch("teatree.config.get_effective_settings", return_value=settings),
            patch("teatree.core.self_forge_identities.declared_identities_for_url", return_value=()),
        ):
            resolved = self_identity_set(_GITLAB_ISSUE, host=_FakeHost({}))
        assert resolved == {"adrien.cossa"}


class TestDeclaredIdentitiesAreReadOnce(TestCase):
    """One read of ``self_forge_identities``, shared by every consumer of it.

    Three hand-rolled reads of one setting is how they drift — and they had already
    drifted on case, which is why the shared reader preserves it and each caller folds
    to suit its own comparison. The push gate lowercases; the review-candidate
    self-author check is case-SENSITIVE and would silently narrow if folded for it.
    """

    def test_it_reads_the_setting_for_the_urls_host(self) -> None:
        with patch(
            "teatree.hooks.foreign_mr_cli.cold_reader.mapping_setting",
            return_value={"gitlab.com": ["Declared-Bot", " spaced ", 7, ""]},
        ):
            assert declared_identities_for_url(_GITLAB_ISSUE) == ("Declared-Bot", "spaced")

    def test_case_is_preserved_for_the_case_sensitive_consumer(self) -> None:
        with patch(
            "teatree.hooks.foreign_mr_cli.cold_reader.mapping_setting",
            return_value={"github.com": ["Factory-Bot"]},
        ):
            assert declared_identities_for_url(_GITHUB_ISSUE) == ("Factory-Bot",)

    def test_the_push_gate_folds_the_same_rows_itself(self) -> None:
        with patch(
            "teatree.hooks.foreign_mr_cli.cold_reader.mapping_setting",
            return_value={"github.com": ["Factory-Bot"]},
        ):
            assert declared_self_identities("github.com") == frozenset({"factory-bot"})

    def test_a_host_with_no_declaration_declares_nothing(self) -> None:
        with patch(
            "teatree.hooks.foreign_mr_cli.cold_reader.mapping_setting", return_value={"gitlab.com": "not-a-list"}
        ):
            assert declared_identities_for_url(_GITLAB_ISSUE) == ()

    def test_a_urlless_string_declares_nothing_rather_than_widening(self) -> None:
        assert declared_identities_for_url("") == ()


class TestIssueAuthorIsSelf(TestCase):
    def test_case_folds_both_sides(self) -> None:
        assert issue_author_is_self("Adrien.Cossa", {"adrien.cossa"}) is True
        assert issue_author_is_self("adrien.cossa", {"ADRIEN.COSSA"}) is True

    def test_unknown_author_is_not_self(self) -> None:
        assert issue_author_is_self("colleague", {"adrien.cossa"}) is False

    def test_empty_author_fails_closed(self) -> None:
        assert issue_author_is_self("", {"adrien.cossa"}) is False
        assert issue_author_is_self("   ", {"adrien.cossa"}) is False


class TestRequireSelfAuthoredIssue(TestCase):
    def _identities(self, *names: str) -> AbstractContextManager[object]:
        return patch("teatree.core.self_forge_identities.self_identity_set", return_value=set(names))

    def test_owner_authored_issue_is_allowed_and_returns_the_fresh_payload(self) -> None:
        host = _FakeHost(_gitlab_payload("adrien.cossa"))
        with self._identities("adrien.cossa"):
            fresh = require_self_authored_issue(host=host, issue_url=_GITLAB_ISSUE)
        assert fresh["author"] == {"username": "adrien.cossa"}
        assert host.calls == [_GITLAB_ISSUE], "authority comes from a fetch at write time, never a cached candidate"

    def test_factory_bot_authored_issue_is_allowed(self) -> None:
        host = _FakeHost(_github_payload("declared-bot"))
        with self._identities("declared-bot"):
            assert require_self_authored_issue(host=host, issue_url=_GITHUB_ISSUE)

    def test_colleague_authored_issue_is_refused(self) -> None:
        host = _FakeHost(_gitlab_payload("someone.else"))
        with self._identities("adrien.cossa"), pytest.raises(ExternalIssueRefusedError) as caught:
            require_self_authored_issue(host=host, issue_url=_GITLAB_ISSUE)
        assert caught.value.author == "someone.else"
        assert _GITLAB_ISSUE in str(caught.value)

    def test_absent_author_is_refused(self) -> None:
        host = _FakeHost({"web_url": _GITLAB_ISSUE})
        with self._identities("adrien.cossa"), pytest.raises(ExternalIssueRefusedError):
            require_self_authored_issue(host=host, issue_url=_GITLAB_ISSUE)

    def test_unreadable_issue_is_refused_not_assumed_ours(self) -> None:
        host = _FakeHost(RuntimeError("forge down"))
        with self._identities("adrien.cossa"), pytest.raises(ExternalIssueRefusedError):
            require_self_authored_issue(host=host, issue_url=_GITLAB_ISSUE)

    def test_error_payload_is_refused(self) -> None:
        host = _FakeHost({"error": "Issue not found"})
        with self._identities("adrien.cossa"), pytest.raises(ExternalIssueRefusedError):
            require_self_authored_issue(host=host, issue_url=_GITLAB_ISSUE)
