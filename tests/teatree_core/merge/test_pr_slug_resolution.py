"""Registry-backed evidence for "is this CLEAR slug a repo?" (#4249).

``_looks_like_owner_repo`` judges from string shape, so a head branch whose first
segment is not a known git prefix (``review-fixes/docs``) reads as ``owner/repo``.
:func:`slug_is_registered_repo` answers the same question from the registry
enumeration instead — the evidence the shape test cannot have.
"""

from dataclasses import dataclass

import pytest

from teatree.core.merge import pr_slug_resolution
from teatree.core.merge.pr_slug_resolution import (
    fallback_repo_slug,
    known_repo_slugs,
    resolve_pr_repo_slug,
    slug_is_registered_repo,
)


@dataclass(frozen=True, slots=True)
class _Ticket:
    issue_url: str


@dataclass(frozen=True, slots=True)
class _Clear:
    slug: str
    pr_id: int = 4230
    ticket: _Ticket | None = None


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The registry enumeration, swapped for an explicit list the test controls."""
    slugs: list[str] = ["souliane/teatree"]
    monkeypatch.setattr(pr_slug_resolution, "_iter_candidate_repo_slugs", lambda: list(slugs))
    return slugs


class TestSlugIsRegisteredRepo:
    def test_a_registered_repo_is_named(self, registry: list[str]) -> None:
        _ = registry
        assert slug_is_registered_repo("souliane/teatree") is True

    def test_a_head_branch_the_shape_test_accepts_is_not_named(self, registry: list[str]) -> None:
        _ = registry
        assert pr_slug_resolution._looks_like_owner_repo("review-fixes/docs") is True
        assert slug_is_registered_repo("review-fixes/docs") is False

    def test_a_workstream_slug_is_not_named(self, registry: list[str]) -> None:
        _ = registry
        assert slug_is_registered_repo("statusline-stale-wakeup") is False

    def test_an_empty_registry_names_nothing(self, registry: list[str]) -> None:
        registry.clear()
        assert known_repo_slugs() == frozenset()
        assert slug_is_registered_repo("souliane/teatree") is False

    def test_a_declared_working_repo_url_is_named_after_canonicalization(self, registry: list[str]) -> None:
        registry.append("downstream-org/companion")
        assert slug_is_registered_repo("https://github.com/downstream-org/companion.git") is True


class TestFallbackRepoSlug:
    def test_the_ticket_issue_url_wins_over_the_clone_origin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pr_slug_resolution, "_project_repo_slug", lambda: "souliane/teatree")
        clear = _Clear(slug="review-fixes/docs", ticket=_Ticket("https://github.com/downstream-org/app/issues/7"))
        assert fallback_repo_slug(clear) == "downstream-org/app"

    def test_falls_through_to_the_clone_origin_without_a_ticket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pr_slug_resolution, "_project_repo_slug", lambda: "souliane/teatree")
        assert fallback_repo_slug(_Clear(slug="statusline-stale-wakeup")) == "souliane/teatree"

    def test_resolve_pr_repo_slug_still_walks_ticket_then_clone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pr_slug_resolution, "_project_repo_slug", lambda: "souliane/teatree")
        workstream = _Clear(slug="statusline-stale-wakeup")
        assert resolve_pr_repo_slug(workstream) == "souliane/teatree"
        with_ticket = _Clear(slug="statusline-stale-wakeup", ticket=_Ticket("https://github.com/o/r/issues/1"))
        assert resolve_pr_repo_slug(with_ticket) == "o/r"
