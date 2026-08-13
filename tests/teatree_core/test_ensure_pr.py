"""Tests for the ensure-pr helpers (mirrors ``_ensure_pr``).

Split out of ``test_pr_command`` alongside the ``_ensure_pr`` module
extraction: test files mirror the production module path. The behavioural
``ensure-pr`` command tests (PUSHED_ORPHAN / pre-push-deadlock deferral)
stay in ``test_pr_command`` because they drive ``call_command("pr",
"ensure-pr")`` end to end.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState, PullRequestSpec
from teatree.core.management.commands import _ensure_pr as ensure_pr_mod
from teatree.core.management.commands._ensure_pr import _ticket_extra_for_branch, create_or_defer_pr
from teatree.core.merge.pr_url_record import record_pr_url
from teatree.core.models import PullRequest, Ticket, Worktree
from teatree.types import RawAPIDict
from teatree.utils.run import CommandFailedError, run_checked
from tests.teatree_core.pr_command._shared import _MOCK_OVERLAY


class TestTicketExtraForBranch(TestCase):
    """Resolve the owning ticket's ``extra`` from the orphan-branch name (#873).

    Lets the pre-push ``ensure-pr`` fallback honor the explicit
    ``more_prs_coming`` opt-out even though it has no ticket handle.
    """

    def test_returns_none_when_no_worktree_for_branch(self) -> None:
        assert _ticket_extra_for_branch("no-such-branch") is None

    def test_returns_ticket_extra_for_known_branch(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://github.com/souliane/teatree/issues/873",
            extra={"more_prs_coming": True},
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/repo873",
            branch="fix/873-x",
            extra={"worktree_path": "/tmp/repo873"},
        )
        assert _ticket_extra_for_branch("fix/873-x") == {"more_prs_coming": True}

    def test_returns_latest_worktree_when_branch_reused(self) -> None:
        old = Ticket.objects.create(overlay="test", issue_url="https://x/1", extra={"more_prs_coming": True})
        new = Ticket.objects.create(overlay="test", issue_url="https://x/2", extra={})
        Worktree.objects.create(ticket=old, overlay="test", repo_path="/tmp/a", branch="shared")
        Worktree.objects.create(ticket=new, overlay="test", repo_path="/tmp/b", branch="shared")
        assert _ticket_extra_for_branch("shared") == {}


class NonAssignableHost:
    """gh with a pull-only PAT: the login resolves but cannot be assigned (#3100)."""

    def __init__(self) -> None:
        self.created_specs: list[PullRequestSpec] = []

    def current_user(self) -> str:
        return "pullonly-bot"

    def is_assignable(self, *, repo: str, login: str) -> bool:
        return False

    def create_pr(self, spec: PullRequestSpec) -> RawAPIDict:
        if spec.assignee:
            raise CommandFailedError(
                ["gh", "pr", "create"],
                1,
                "",
                f"could not assign user: '{spec.assignee}' not found",
            )
        self.created_specs.append(spec)
        return {"web_url": "https://github.com/souliane/teatree/pull/9999"}

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        return PrOpenState.OPEN


class TestNonAssignableIdentityRegression3100(TestCase):
    """``pr create`` must succeed when the host-token login is not assignable."""

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._monkeypatch = monkeypatch
        self._tmp_path = tmp_path

    def _orphan_repo(self) -> Path:
        origin = self._tmp_path / "origin.git"
        run_checked(["git", "init", "--bare", str(origin)])
        work = self._tmp_path / "work"
        run_checked(["git", "init", "-b", "main", str(work)])
        run_checked(["git", "config", "user.email", "agent@users.noreply.github.com"], cwd=work)
        run_checked(["git", "config", "user.name", "agent"], cwd=work)
        run_checked(["git", "remote", "add", "origin", str(origin)], cwd=work)
        (work / "README.md").write_text("seed\n")
        run_checked(["git", "add", "-A"], cwd=work)
        run_checked(["git", "commit", "-m", "seed"], cwd=work)
        run_checked(["git", "push", "-u", "origin", "main"], cwd=work)
        run_checked(["git", "checkout", "-b", "fix/3100-x"], cwd=work)
        (work / "fix.py").write_text("x = 1\n")
        run_checked(["git", "add", "-A"], cwd=work)
        run_checked(["git", "commit", "-m", "fix(core): own commit"], cwd=work)
        return work

    def test_create_succeeds_without_assignee(self) -> None:
        host = NonAssignableHost()
        self._monkeypatch.setattr(ensure_pr_mod, "code_host_for_repo_from_overlay", lambda repo_path: host)
        repo = self._orphan_repo()

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = create_or_defer_pr(str(repo), "fix/3100-x")

        assert result.get("url") == "https://github.com/souliane/teatree/pull/9999"
        assert host.created_specs[0].assignee == ""


class AssignableHost:
    """A host that opens the PR and confirms it on re-read — the ordinary create."""

    URL = "https://github.com/souliane/teatree/pull/4305"

    def current_user(self) -> str:
        return "souliane"

    def is_assignable(self, *, repo: str, login: str) -> bool:
        return True

    def create_pr(self, spec: PullRequestSpec) -> RawAPIDict:
        return {"web_url": self.URL}

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        return PrOpenState.OPEN


class TestEnsurePrRecordsTheVerifiedUrl(TestCase):
    """#4305: the PR this hook opens lands on the ticket, so a later refusal reconciles.

    ``ensure-pr`` runs inside the git PRE-push hook, so its PR is already live by
    the time any post-push refusal fires. Unrecorded, that PR was invisible to the
    retry, which then collided with ``already exists``.
    """

    BRANCH = "fix/4305-x"

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._monkeypatch = monkeypatch
        self._tmp_path = tmp_path

    def _repo(self) -> Path:
        origin = self._tmp_path / "origin.git"
        run_checked(["git", "init", "--bare", str(origin)])
        work = self._tmp_path / "work"
        run_checked(["git", "init", "-b", "main", str(work)])
        run_checked(["git", "config", "user.email", "agent@example.com"], cwd=work)
        run_checked(["git", "config", "user.name", "agent"], cwd=work)
        run_checked(["git", "remote", "add", "origin", str(origin)], cwd=work)
        (work / "README.md").write_text("seed\n")
        run_checked(["git", "add", "-A"], cwd=work)
        run_checked(["git", "commit", "-m", "seed"], cwd=work)
        run_checked(["git", "push", "-u", "origin", "main"], cwd=work)
        run_checked(["git", "checkout", "-b", self.BRANCH], cwd=work)
        (work / "fix.py").write_text("x = 1\n")
        run_checked(["git", "add", "-A"], cwd=work)
        run_checked(["git", "commit", "-m", "fix(core): own commit"], cwd=work)
        return work

    def _create(self, repo: Path) -> dict:
        self._monkeypatch.setattr(ensure_pr_mod, "code_host_for_repo_from_overlay", lambda repo_path: AssignableHost())
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            return dict(create_or_defer_pr(str(repo), self.BRANCH))

    def _owning_ticket(self, repo: Path) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://github.com/souliane/teatree/issues/4305")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path=str(repo),
            branch=self.BRANCH,
            extra={"worktree_path": str(repo)},
        )
        return ticket

    def test_verified_url_lands_on_the_owning_ticket(self) -> None:
        repo = self._repo()
        ticket = self._owning_ticket(repo)

        assert self._create(repo).get("url") == AssignableHost.URL

        ticket.refresh_from_db()
        assert ticket.extra["pr_urls"] == [AssignableHost.URL]
        assert ticket.extra["pr_url_by_branch"] == {self.BRANCH: AssignableHost.URL}

    def test_the_arbiter_row_is_written_too(self) -> None:
        # The JSON index is the ticket's own cache; the merge keystone and the board
        # reconcile resolve a PR's owning ticket through the `PullRequest` row.
        repo = self._repo()
        ticket = self._owning_ticket(repo)

        self._create(repo)

        row = PullRequest.objects.get(url=AssignableHost.URL)
        assert row.ticket_id == ticket.pk

    def test_a_genuinely_orphan_branch_records_nothing(self) -> None:
        # No worktree row names this branch, so there is no ticket to record against.
        repo = self._repo()

        assert self._create(repo).get("url") == AssignableHost.URL

        assert not PullRequest.objects.filter(url=AssignableHost.URL).exists()

    def test_a_create_that_returned_no_url_records_nothing(self) -> None:
        # An unverifiable create is the case the #1226 error return exists for;
        # recording it would put a phantom PR on the ticket.
        repo = self._repo()
        ticket = self._owning_ticket(repo)
        self._monkeypatch.setattr(AssignableHost, "create_pr", lambda self, spec: {})

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = self._create(repo)

        assert "no PR url" in result["error"]
        ticket.refresh_from_db()
        assert "pr_urls" not in ticket.extra
        assert not PullRequest.objects.exists()

    def test_recording_twice_neither_duplicates_nor_raises(self) -> None:
        # The ship records the same URL after its own create; the two paths converge.
        repo = self._repo()
        ticket = self._owning_ticket(repo)

        self._create(repo)
        record_pr_url(ticket, AssignableHost.URL, self.BRANCH)

        ticket.refresh_from_db()
        assert ticket.extra["pr_urls"] == [AssignableHost.URL]
        assert PullRequest.objects.filter(url=AssignableHost.URL).count() == 1
