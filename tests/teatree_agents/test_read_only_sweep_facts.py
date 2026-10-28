"""The worker inlines a bounded open-issue index for a read-only sweep."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.agents.read_only_venue_brief import ReadOnlyBriefRefusedError, _sweep_issue_facts
from teatree.core.models import Session, Task, Ticket


class TestSweepFacts(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="example", issue_url="backlog-sweep://example")
        self.task = Task.objects.create(
            ticket=self.ticket,
            session=Session.objects.create(ticket=self.ticket),
            phase="backlog_sweep",
            execution_reason="ASK-GATE: return proposals",
        )
        self.issues = [
            {
                "html_url": f"https://github.com/example-org/example-repo/issues/{n}",
                "title": f"Issue {n}",
                "user": {"login": "owner" if n != 2 else "someone-else"},
            }
            for n in range(1, 24)
        ]
        self.host = SimpleNamespace(
            list_repo_open_issues=lambda *, repo: self.issues,
            current_user=lambda: "owner",
            get_repo=lambda *, repo: {"name": repo},
        )

    def _facts(self) -> tuple[str, str]:
        provider = SimpleNamespace(get_code_host_for_repo=lambda _overlay, _repo: self.host)
        with (
            patch("teatree.agents.read_only_venue_brief.get_backend_provider", return_value=provider),
            patch("teatree.agents.read_only_venue_brief.get_overlay_for_ticket", return_value="overlay"),
            patch("teatree.agents.read_only_venue_brief.owned_repo_slugs", return_value=("example-org/example-repo",)),
        ):
            return _sweep_issue_facts(self.task)

    def test_index_flags_external_authors_and_bounds_a_twenty_issue_window(self) -> None:
        index, first = self._facts()

        assert "ext" in index.splitlines()[1]
        assert "/issues/1" in first
        assert "/issues/21" not in first

    def test_failed_sweep_advances_the_next_task_window(self) -> None:
        _, first = self._facts()
        self.task = Task.objects.create(
            ticket=self.ticket,
            session=Session.objects.create(ticket=self.ticket),
            phase="backlog_sweep",
            execution_reason="ASK-GATE: return proposals",
        )
        _, second = self._facts()

        assert "/issues/1" in first
        assert "/issues/21" in second
        assert "/issues/1" not in second

    def test_missing_ask_gate_refuses_before_listing(self) -> None:
        self.task.execution_reason = "unguarded sweep"

        with pytest.raises(ReadOnlyBriefRefusedError, match="ASK-GATE"):
            self._facts()

    def test_listing_failure_refuses_instead_of_returning_an_empty_index(self) -> None:
        def unreadable(*, repo: str) -> list[dict[str, object]]:
            raise RuntimeError(repo)

        self.host.list_repo_open_issues = unreadable
        with pytest.raises(ReadOnlyBriefRefusedError, match="could not be read"):
            self._facts()

    def test_oversized_index_is_refused_instead_of_truncated(self) -> None:
        self.issues = [
            {
                "html_url": f"https://github.com/example-org/example-repo/issues/{n}",
                "title": "x" * 80,
                "user": {"login": "owner"},
            }
            for n in range(1, 1001)
        ]

        with pytest.raises(ReadOnlyBriefRefusedError, match="over budget"):
            self._facts()

    def test_window_labels_and_bounds_a_body_excerpt(self) -> None:
        self.issues[0]["body"] = "x" * (64 * 1024)

        _, window = self._facts()

        assert "BODY EXCERPT:" in window
        assert len(window.encode()) < 4 * 1024

    def test_unresolved_repo_cannot_look_like_an_empty_tracker(self) -> None:
        self.issues = []
        self.host.get_repo = lambda *, repo: {"error": f"unresolved {repo}"}

        with pytest.raises(ReadOnlyBriefRefusedError, match="could not be read"):
            self._facts()

    def test_issue_slug_casing_does_not_refuse_its_owned_repo(self) -> None:
        self.issues[0]["html_url"] = "https://github.com/Example-Org/Example-Repo/issues/1"

        index, _ = self._facts()

        assert self.issues[0]["html_url"] in index

    def test_issue_url_with_query_refuses_as_invalid_identity(self) -> None:
        self.issues[0]["html_url"] += "?page=1"

        with pytest.raises(ReadOnlyBriefRefusedError, match="could not be read"):
            self._facts()
