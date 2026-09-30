"""The provision-failure question: its marker, its no-repos retry budget, its text, its heal proof (#4904)."""

import datetime as dt
import tempfile
from pathlib import Path

from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.provision.failure_question import (
    NO_REPOS_RETRY_DELAYS,
    no_repos_retry_delay,
    provision_failure_marker,
    provision_failures_healed,
    record_provision_failure_question,
)
from tests._git_repo import make_git_repo


def _ticket(*, repos: list[str], issue_url: str = "https://example.com/issues/7") -> Ticket:
    return Ticket.objects.create(overlay="test", issue_url=issue_url, repos=repos, state=Ticket.State.WORK_STARTED)


def _checkout(ticket: Ticket, repo: str, path: Path | str) -> Worktree:
    return Worktree.objects.create(ticket=ticket, repo_path=repo, branch="7-x", extra={"worktree_path": str(path)})


def test_the_marker_names_the_ticket_pk() -> None:
    assert provision_failure_marker(42) == "provision-failure:42"


def test_the_retry_budget_widens_and_ends() -> None:
    assert [no_repos_retry_delay(i) for i in range(len(NO_REPOS_RETRY_DELAYS))] == list(NO_REPOS_RETRY_DELAYS)
    assert list(NO_REPOS_RETRY_DELAYS) == sorted(NO_REPOS_RETRY_DELAYS)
    assert no_repos_retry_delay(len(NO_REPOS_RETRY_DELAYS)) is None
    assert sum(NO_REPOS_RETRY_DELAYS, dt.timedelta()) < dt.timedelta(hours=24)


def test_a_negative_attempt_has_no_delay() -> None:
    assert no_repos_retry_delay(-1) is None


class TestQuestionText(TestCase):
    def test_a_repo_less_ticket_is_offered_only_the_options_that_apply(self) -> None:
        ticket = _ticket(repos=[])

        question = record_provision_failure_question(ticket, "no repos on ticket", retries=6)

        assert question.dedupe_marker == f"provision-failure:{ticket.pk}"
        assert "t3 test workspace ticket https://example.com/issues/7" in question.question
        assert "ignore the ticket" in question.question
        assert "after 6 automatic retries" in question.question
        assert "drop the repo" not in question.question
        assert "holds at STARTED" not in question.question

    def test_a_ticket_with_repos_keeps_the_existing_text(self) -> None:
        ticket = _ticket(repos=["backend"])

        question = record_provision_failure_question(ticket, "failed to create worktrees for: backend")

        assert question.question == (
            "Provision failed on https://example.com/issues/7 (overlay test): failed to create worktrees for: "
            "backend. Every repo must provision before planning starts, so the ticket holds at STARTED until this "
            "one does. Retry checkout creation from the venue that owns the worktrees "
            "(`t3 test workspace ticket https://example.com/issues/7`), or drop the repo from the ticket?"
        )

    def test_a_repeat_failure_returns_the_pending_row(self) -> None:
        ticket = _ticket(repos=[])

        first = record_provision_failure_question(ticket, "no repos on ticket", retries=6)
        second = record_provision_failure_question(ticket, "no repos on ticket", retries=6)

        assert first.pk == second.pk


class TestHealProof(TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _live(self, name: str) -> Path:
        return make_git_repo(self.root / name)

    def _asked(self, ticket: Ticket) -> DeferredQuestion:
        return record_provision_failure_question(ticket, "no repos on ticket", retries=6)

    def test_every_repo_live_heals_the_question(self) -> None:
        ticket = _ticket(repos=["backend", "frontend"])
        question = self._asked(ticket)
        _checkout(ticket, "backend", self._live("backend"))
        _checkout(ticket, "frontend", self._live("frontend"))

        healed = provision_failures_healed([question])

        assert set(healed) == {question.pk}
        assert "backend, frontend" in healed[question.pk]

    def test_one_repo_short_keeps_the_question(self) -> None:
        ticket = _ticket(repos=["backend", "frontend"])
        question = self._asked(ticket)
        _checkout(ticket, "backend", self._live("backend"))

        assert provision_failures_healed([question]) == {}

    def test_a_ticket_still_without_repos_keeps_the_question(self) -> None:
        question = self._asked(_ticket(repos=[]))

        assert provision_failures_healed([question]) == {}

    def test_an_empty_recorded_path_keeps_the_question(self) -> None:
        ticket = _ticket(repos=["backend"])
        question = self._asked(ticket)
        _checkout(ticket, "backend", "")

        assert provision_failures_healed([question]) == {}

    def test_a_recorded_path_whose_checkout_vanished_keeps_the_question(self) -> None:
        # The provisioner keeps a reused row's stale path when re-provisioning it fails,
        # so a row alone is a claim, not a checkout.
        ticket = _ticket(repos=["backend"])
        question = self._asked(ticket)
        _checkout(ticket, "backend", self.root / "gone")

        assert provision_failures_healed([question]) == {}

    def test_a_deleted_ticket_keeps_the_question(self) -> None:
        ticket = _ticket(repos=["backend"])
        question = self._asked(ticket)
        _checkout(ticket, "backend", self._live("backend"))
        Ticket.objects.filter(pk=ticket.pk).delete()

        assert provision_failures_healed([question]) == {}

    def test_another_marker_is_never_healed(self) -> None:
        ticket = _ticket(repos=["backend"])
        _checkout(ticket, "backend", self._live("backend"))
        other = DeferredQuestion.record("Intake held", dedupe_marker=f"attachment-hold:{ticket.pk}")
        near_miss = DeferredQuestion.record("Near miss", dedupe_marker=f"provision-failure:{ticket.pk}x")

        assert provision_failures_healed([other, near_miss]) == {}
