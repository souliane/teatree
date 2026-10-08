"""Step 3a of the architectural-review skill: the pass owns a per-pass ticket before it pushes."""

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState, PullRequestSpec
from teatree.core.management.commands import _ensure_pr as ensure_pr_mod
from teatree.core.management.commands._ensure_pr import create_or_defer_pr
from teatree.core.merge.ticket_resolution import gated_ticket_for_review_task
from teatree.core.models import AutoReviewDispatch, PullRequest, Rubric, Ticket, Worktree
from teatree.core.models.rubric import PHASE_CRITERIA
from teatree.types import RawAPIDict
from tests._git_repo import make_git_repo, run_git
from tests.teatree_core.pr_command._shared import _MOCK_OVERLAY

_SKILL = Path(__file__).resolve().parents[2] / "skills" / "architectural-review" / "SKILL.md"
_BRANCH = "review-fixes/2026-10-07"
_PR_URL = "https://github.com/souliane/teatree/pull/5149"
_OVERLAY_NAME_FOR_CWD = "teatree.core.management.commands._worktree_adopt_command._overlay_name_for_cwd"


def _step_3a() -> str:
    text = _SKILL.read_text(encoding="utf-8")
    start = text.index("**3a.**")
    return text[start : text.index("\n4. ", start)]


def test_architectural_review_step_adopts_then_visits_coding_and_never_names_a_ticket() -> None:
    step = _step_3a()

    assert "`t3 <overlay> worktree adopt <abs path> --json`" in step
    assert "`mcp__teatree__ticket_visit_phase <ticket_id> coding`" in step
    assert "never pass `--ticket`" in step


class _Host:
    def current_user(self) -> str:
        return "souliane"

    def is_assignable(self, *, repo: str, login: str) -> bool:
        return True

    def create_pr(self, spec: PullRequestSpec) -> RawAPIDict:
        return {"web_url": _PR_URL}

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        return PrOpenState.OPEN


class TestArchitecturalReviewStepChain(TestCase):
    """adopt -> visit coding -> ensure-pr records the PR on that ticket, which then owns the rubric."""

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._monkeypatch = monkeypatch
        self._tmp = tmp_path

    def _linked_checkout_with_a_commit(self) -> Path:
        origin = self._tmp / "origin.git"
        run_git(self._tmp, "init", "--bare", str(origin))
        clone = make_git_repo(self._tmp / "teatree-deploy")
        run_git(clone, "remote", "add", "origin", str(origin))
        run_git(clone, "push", "-u", "origin", "main")
        checkout = self._tmp / "architectural-review"
        run_git(clone, "worktree", "add", "-b", _BRANCH, "--no-track", str(checkout))
        (checkout / "fix.py").write_text("x = 1\n")
        run_git(checkout, "add", "-A")
        run_git(checkout, "commit", "-m", "fix(core): the pass's own commit")
        return checkout

    def _bystander_tickets(self) -> tuple[Ticket, Ticket]:
        anchor = Ticket.objects.create(overlay="test", issue_url="architectural-review://test")
        catch_all = Ticket.objects.create(overlay="test", issue_url="auto:HEAD")
        sibling = self._tmp / "cold-review"
        sibling.mkdir()
        Worktree.objects.create(
            ticket=catch_all,
            overlay="test",
            repo_path="cold-review",
            branch="HEAD",
            extra={"worktree_path": str(sibling)},
        )
        return anchor, catch_all

    def test_the_pass_pr_lands_on_its_own_ticket_with_the_coding_criterion(self) -> None:
        anchor, catch_all = self._bystander_tickets()
        checkout = self._linked_checkout_with_a_commit()
        adopted = StringIO()

        with patch(_OVERLAY_NAME_FOR_CWD, return_value="test"):
            call_command("worktree", "adopt", str(checkout), "--json", stdout=adopted)
        ticket = Ticket.objects.get(pk=json.loads(adopted.getvalue())["ticket_id"])
        call_command("lifecycle", "visit-phase", str(ticket.pk), "coding", agent_id="architectural-review-test")
        self._monkeypatch.setattr(ensure_pr_mod, "code_host_for_repo_from_overlay", lambda repo_path: _Host())
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            created = create_or_defer_pr(str(checkout), _BRANCH)

        assert created.get("url") == _PR_URL, created
        assert ticket.issue_url == f"auto:{_BRANCH}"
        assert PullRequest.objects.get(url=_PR_URL).ticket_id == ticket.pk
        dispatch = AutoReviewDispatch.enqueue(
            slug="souliane/teatree", pr_id=5149, head_sha="a" * 40, pr_url=_PR_URL, overlay="test"
        )
        assert dispatch is not None
        assert dispatch.task is not None
        assert gated_ticket_for_review_task(dispatch.task) == ticket
        rubric = Rubric.objects.active_for_ticket(ticket)
        assert rubric is not None
        assert PHASE_CRITERIA["coding"] in set(rubric.criteria.values_list("text", flat=True))
        assert not Worktree.objects.filter(ticket=anchor).exists()
        assert Worktree.objects.filter(ticket=catch_all).count() == 1
