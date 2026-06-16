"""Tests for the deterministic, code-only reference linkifier.

``teatree.core.reference_linkifier`` is the deterministic alternative to the
model-driven bare-reference link gate: it resolves each bare ref to a
canonical URL from teatree's own DB (``PullRequest`` ref->URL store,
``Ticket.issue_url``) with a repo-context construction fallback, then rewrites
the message to markdown ``[ref](url)`` in code — no model round-trip.

The pure-function tests (``linkify``) use synthetic resolvers; the resolver
tests exercise the real DB-hit / construction-fallback / leave-untouched
contract against a synthetic ``acme/widgets`` repo.
"""

import pytest

from teatree.core.models import PullRequest, Ticket
from teatree.core.reference_linkifier import ReferenceResolver, linkify

_BASE = "https://github.com/acme/widgets"
_GLAB_BASE = "https://gitlab.com/acme/widgets"


def _mr(n: int) -> str | None:
    table = {281: f"{_BASE}/pull/281", 381: f"{_GLAB_BASE}/-/merge_requests/381"}
    return table.get(n)


def _issue(n: int) -> str | None:
    table = {1011: f"{_BASE}/issues/1011"}
    return table.get(n)


def _slug_issue(slug: str, n: int) -> str | None:
    table = {("other/repo", 42): "https://github.com/other/repo/issues/42"}
    return table.get((slug, n))


class TestLinkifyEachRefFormat:
    def test_mr_token_rewritten_to_markdown_link(self) -> None:
        assert linkify("ship !281 now", mr_resolver=_mr) == f"ship [!281]({_BASE}/pull/281) now"

    def test_issue_token_rewritten_to_markdown_link(self) -> None:
        assert linkify("fixes #1011", issue_resolver=_issue) == f"fixes [#1011]({_BASE}/issues/1011)"

    def test_cross_repo_owner_slash_repo_issue_rewritten(self) -> None:
        out = linkify("see other/repo#42 too", slug_issue_resolver=_slug_issue)
        assert out == "see [other/repo#42](https://github.com/other/repo/issues/42) too"

    def test_multiple_tokens_in_one_line(self) -> None:
        out = linkify("!281 and #1011", mr_resolver=_mr, issue_resolver=_issue)
        assert f"[!281]({_BASE}/pull/281)" in out
        assert f"[#1011]({_BASE}/issues/1011)" in out

    def test_cross_repo_ref_not_split_into_bare_issue(self) -> None:
        # ``other/repo#42`` must resolve as a slug ref, never as a bare ``#42``
        # against the wrong (active) repo.
        out = linkify("other/repo#42", issue_resolver=lambda n: f"{_BASE}/issues/{n}", slug_issue_resolver=_slug_issue)
        assert out == "[other/repo#42](https://github.com/other/repo/issues/42)"


class TestLinkifyUnresolvableLeftUntouched:
    def test_unknown_mr_left_bare(self) -> None:
        assert linkify("ship !999", mr_resolver=_mr) == "ship !999"

    def test_unknown_issue_left_bare(self) -> None:
        assert linkify("fixes #9999", issue_resolver=_issue) == "fixes #9999"

    def test_no_resolver_leaves_tokens_bare(self) -> None:
        assert linkify("!281 and #1011") == "!281 and #1011"


class TestLinkifySkipsLinkedAndCode:
    def test_already_linked_ref_not_double_wrapped(self) -> None:
        text = f"[!281]({_BASE}/pull/281)"
        assert linkify(text, mr_resolver=_mr) == text

    def test_ref_inside_inline_code_skipped(self) -> None:
        assert linkify("use `!281` literally", mr_resolver=_mr) == "use `!281` literally"

    def test_ref_inside_fenced_block_skipped(self) -> None:
        text = "before\n```\nsee !281 here\n```\nafter"
        assert linkify(text, mr_resolver=_mr) == text

    def test_ref_in_link_label_not_corrupted(self) -> None:
        # A ``#1011`` living inside an existing link label must not be
        # rewritten into a nested link.
        text = f"[issue #1011]({_BASE}/issues/1011)"
        assert linkify(text, issue_resolver=_issue) == text


class TestLinkifyIdempotent:
    def test_double_application_is_noop(self) -> None:
        once = linkify("ship !281 and #1011", mr_resolver=_mr, issue_resolver=_issue)
        twice = linkify(once, mr_resolver=_mr, issue_resolver=_issue)
        assert once == twice

    def test_empty_string(self) -> None:
        assert linkify("", mr_resolver=_mr) == ""


class TestReferenceResolverConstruction:
    def test_github_issue_constructed(self) -> None:
        resolver = ReferenceResolver(code_host="github", default_slug="acme/widgets", web_base="https://github.com")
        assert resolver.resolve_issue(7) == "https://github.com/acme/widgets/issues/7"

    def test_github_mr_uses_issues_path_redirect(self) -> None:
        resolver = ReferenceResolver(code_host="github", default_slug="acme/widgets", web_base="https://github.com")
        assert resolver.resolve_mr(7) == "https://github.com/acme/widgets/issues/7"

    def test_gitlab_mr_constructed(self) -> None:
        resolver = ReferenceResolver(code_host="gitlab", default_slug="acme/widgets", web_base="https://gitlab.com")
        assert resolver.resolve_mr(7) == "https://gitlab.com/acme/widgets/-/merge_requests/7"

    def test_gitlab_issue_constructed(self) -> None:
        resolver = ReferenceResolver(code_host="gitlab", default_slug="acme/widgets", web_base="https://gitlab.com")
        assert resolver.resolve_issue(7) == "https://gitlab.com/acme/widgets/-/issues/7"

    def test_no_repo_context_yields_none(self) -> None:
        resolver = ReferenceResolver(code_host="github")
        assert resolver.resolve_issue(7) is None

    def test_unknown_host_yields_none(self) -> None:
        resolver = ReferenceResolver(code_host="bitbucket", default_slug="acme/widgets", web_base="https://x.org")
        assert resolver.resolve_issue(7) is None

    def test_cross_repo_slug_overrides_default(self) -> None:
        resolver = ReferenceResolver(code_host="github", default_slug="acme/widgets", web_base="https://github.com")
        assert resolver.resolve_issue_for_slug("other/repo", 9) == "https://github.com/other/repo/issues/9"


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestReferenceResolverDbHit:
    def test_pull_request_url_wins_over_construction(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        stored = "https://github.com/acme/widgets/pull/281"
        PullRequest.objects.create(ticket=ticket, repo="acme/widgets", iid="281", url=stored)

        resolver = ReferenceResolver(code_host="github", default_slug="acme/widgets", web_base="https://github.com")
        # DB hit returns the stored PR URL (``/pull/281``), not the
        # constructed ``/issues/281``.
        assert resolver.resolve_mr(281) == stored

    def test_ticket_issue_url_hit(self) -> None:
        stored = "https://github.com/acme/widgets/issues/55"
        Ticket.objects.create(overlay="t3-teatree", issue_url=stored)

        resolver = ReferenceResolver(code_host="github", default_slug="acme/widgets", web_base="https://github.com")
        assert resolver.resolve_issue(55) == stored

    def test_db_miss_falls_back_to_construction(self) -> None:
        resolver = ReferenceResolver(code_host="github", default_slug="acme/widgets", web_base="https://github.com")
        # No matching row → constructed URL.
        assert resolver.resolve_issue(999) == "https://github.com/acme/widgets/issues/999"

    def test_pull_request_for_other_repo_not_matched(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        PullRequest.objects.create(
            ticket=ticket, repo="other/repo", iid="281", url="https://github.com/other/repo/pull/281"
        )
        resolver = ReferenceResolver(code_host="github", default_slug="acme/widgets", web_base="https://github.com")
        # The same iid on a DIFFERENT repo must not resolve for acme/widgets;
        # it falls back to construction against the active repo.
        assert resolver.resolve_mr(281) == "https://github.com/acme/widgets/issues/281"
