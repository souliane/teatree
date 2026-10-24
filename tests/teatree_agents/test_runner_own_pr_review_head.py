"""A review of the loop's own pull request is dispatched at the head its checkout and the forge share (#5190).

Real git under a temporary origin and a real ticket checkout; only the forge reads, the process table and
the SDK child are doubles. The SDK double RAISES when a held dispatch opens it, so a guard that holds one
instruction too late fails with the spawn itself rather than with a soft assertion.
"""

import shutil
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.utils import timezone

from teatree.agents import own_pr_review_head as guard
from teatree.agents.own_pr_review_head import HoldCause
from teatree.core.backend_protocols import PrMergeState
from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.modelkit.forge_readability import LiveHeadRead
from teatree.core.modelkit.task_failure_taxonomy import REVIEW_UNRECORDABLE_PREFIX
from teatree.core.models import AutoReviewDispatch, DeferredQuestion, Session, Task, Ticket, Worktree
from teatree.core.models.usage_window_state import REVIEW_HELD_PREFIX
from teatree.core.worktree.checkout_state import checkout_state
from tests._git_repo import make_git_repo, run_git
from tests.factories import TicketFactory, WorktreeFactory
from tests.teatree_agents.test_runner_review_recordability_gate import _ReviewDispatchProbe

_SLUG = "o/r"
_PR_ID = 41
_PR_URL = f"https://github.com/{_SLUG}/pull/{_PR_ID}"
_BRANCH = "feat/own-pr"
_READABLE_TABLE = ProcessTable(frozenset(), "test-table")
_UNREADABLE_TABLE = ProcessTable(frozenset(), "", ("no process table in this venue",))


class _PrRepo:
    """A real origin publishing the PR head under ``refs/pull/41/head``, and a ticket checkout cloned from it."""

    def __init__(self, root: Path) -> None:
        self.origin = make_git_repo(root / "origin.git", bare=True)
        self.author = make_git_repo(root / "author")
        run_git(self.author, "remote", "add", "origin", str(self.origin))
        run_git(self.author, "push", "-q", "origin", "main")
        run_git(self.author, "checkout", "-q", "-b", _BRANCH)
        self.base = self.publish("a.txt")
        self.checkout = root / "checkout"
        run_git(root, "clone", "-q", str(self.origin), str(self.checkout))
        run_git(self.checkout, "checkout", "-q", _BRANCH)
        self.point_origin_at(_SLUG)

    def point_origin_at(self, slug: str) -> None:
        url = f"https://github.com/{slug}.git"
        run_git(self.checkout, "config", "remote.origin.url", url)
        run_git(self.checkout, "config", f"url.{self.origin}.insteadOf", url)

    def publish(self, name: str) -> str:
        (self.author / name).write_text(name)
        run_git(self.author, "add", name)
        run_git(self.author, "commit", "-q", "-m", name)
        run_git(self.author, "push", "-q", "origin", f"HEAD:refs/heads/{_BRANCH}", f"HEAD:refs/pull/{_PR_ID}/head")
        return run_git(self.author, "rev-parse", "HEAD")

    def commit_locally(self, name: str) -> str:
        (self.checkout / name).write_text(name)
        run_git(self.checkout, "add", name)
        run_git(self.checkout, "commit", "-q", "-m", name)
        return self.head()

    def head(self) -> str:
        return run_git(self.checkout, "rev-parse", "HEAD")


@dataclass
class _Forge:
    """What the forge answers about the pull request; an empty ``head``/``state`` is an unreadable read."""

    head: str
    state: str = "OPEN"
    checks: str = "green"

    def query(self) -> SimpleNamespace:
        return SimpleNamespace(
            pr_merge_state=lambda: PrMergeState(state=self.state, merge_commit_oid=""),
            required_checks_status=lambda: self.checks,
        )

    def head_read(self) -> LiveHeadRead:
        return LiveHeadRead(sha=self.head, unreadable=not self.head)


class _OwnPrReviewCase(_ReviewDispatchProbe):
    def setUp(self) -> None:
        super().setUp()
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.repo = _PrRepo(root)

    def own_ticket(self, *, extra: dict[str, object] | None = None) -> Ticket:
        ticket = TicketFactory.create(role=Ticket.Role.AUTHOR, issue_url=_PR_URL, extra=extra or {})
        WorktreeFactory(
            ticket=ticket, repo_path=_SLUG, branch=_BRANCH, extra={"worktree_path": str(self.repo.checkout)}
        )
        return ticket

    def review(
        self,
        forge: _Forge,
        ticket: Ticket | None = None,
        *,
        table: ProcessTable = _READABLE_TABLE,
        phase: str = "reviewing",
        task: Task | None = None,
    ) -> Task:
        with ExitStack() as stack:
            self.for_ref = stack.enter_context(patch.object(guard.CodeHostQuery, "for_ref", return_value=forge.query()))
            self.live_head = stack.enter_context(patch.object(guard, "live_head_at", return_value=forge.head_read()))
            stack.enter_context(patch.object(guard, "read_process_table", return_value=table))
            return self._dispatch(ticket or self.own_ticket(), phase=phase, task=task)

    def reviewed_sha(self, ticket: Ticket) -> str:
        ticket.refresh_from_db()
        return str(ticket.extra.get("reviewed_sha", ""))

    def assert_brief_reviews(self, head: str, file_name: str) -> None:
        brief = self.briefs[-1]
        assert f"the head under review is {head}" in brief
        assert f"+++ b/{file_name}" in brief


class TestACleanCheckoutBehindTheForgeHeadIsAdvanced(_OwnPrReviewCase):
    def test_one_dispatch_moves_the_checkout_stamps_the_head_and_reviews_it(self) -> None:
        head = self.repo.publish("b.txt")
        ticket = self.own_ticket()

        self.review(_Forge(head), ticket)

        assert self.spawned, "a clean checkout one commit behind must be brought level and then reviewed"
        assert self.repo.head() == head
        assert self.reviewed_sha(ticket) == head
        self.assert_brief_reviews(head, "b.txt")

    def test_a_stamped_head_the_checkout_lags_is_brought_level_before_the_brief_is_built(self) -> None:
        head = self.repo.publish("b.txt")
        ticket = self.own_ticket(extra={"reviewed_sha": head})

        self.review(_Forge(head), ticket)

        assert self.repo.head() == head
        self.assert_brief_reviews(head, "b.txt")

    def test_pending_checks_do_not_hold_the_advance(self) -> None:
        head = self.repo.publish("b.txt")

        self.review(_Forge(head, checks="pending"))

        assert self.repo.head() == head
        assert self.spawned

    def test_a_trusted_checkout_stays_trusted_at_the_new_head(self) -> None:
        head = self.repo.publish("b.txt")
        ticket = self.own_ticket()
        worktree = Worktree.objects.get(ticket=ticket)
        worktree.merge_extra(set_keys={"attested_state": checkout_state(self.repo.checkout).digest})

        self.review(_Forge(head), ticket)

        worktree.refresh_from_db()
        assert worktree.get_extra()["attested_state"] == checkout_state(self.repo.checkout).digest

    def test_an_untrusted_checkout_is_not_made_trusted_by_the_advance(self) -> None:
        head = self.repo.publish("b.txt")
        ticket = self.own_ticket()

        self.review(_Forge(head), ticket)

        assert "attested_state" not in Worktree.objects.get(ticket=ticket).get_extra()


class TestAnEqualHeadDispatchesAtOnce(_OwnPrReviewCase):
    def test_the_head_is_stamped_and_the_review_opens_without_reaching_the_remote(self) -> None:
        ticket = self.own_ticket()
        shutil.rmtree(self.repo.origin)

        task = self.review(_Forge(self.repo.base), ticket)

        assert self.spawned
        assert self.reviewed_sha(ticket) == self.repo.base
        assert not task.attempts.filter(error__startswith=REVIEW_HELD_PREFIX).exists()


class TestAnUnsafeCheckoutIsHeldNotMoved(_OwnPrReviewCase):
    harness_must_not_open = True

    def assert_held(self, task: Task, ticket: Ticket, *, head: str, cause: HoldCause, questions: int) -> None:
        task.refresh_from_db()
        assert self.spawned == []
        assert self.repo.head() == head
        assert self.reviewed_sha(ticket) == ""
        assert task.status == Task.Status.PENDING
        assert task.not_before is not None
        assert task.not_before > timezone.now()
        attempt = task.attempts.order_by("-pk").first()
        assert attempt is not None
        assert attempt.error.startswith(REVIEW_HELD_PREFIX)
        assert cause in attempt.error
        assert attempt.result["summary"].startswith(REVIEW_HELD_PREFIX)
        assert (attempt.cost_usd, attempt.num_turns) == (None, None)
        assert DeferredQuestion.objects.count() == questions

    def test_an_edited_tracked_file_is_held_and_asks_the_owner_once(self) -> None:
        head = self.repo.publish("b.txt")
        (self.repo.checkout / "a.txt").write_text("edited")
        ticket = self.own_ticket()
        before = self.repo.head()

        task = self.review(_Forge(head), ticket)

        self.assert_held(task, ticket, head=before, cause=HoldCause.UNCOMMITTED, questions=1)

    def test_an_untracked_file_is_held_and_asks_the_owner_once(self) -> None:
        head = self.repo.publish("b.txt")
        (self.repo.checkout / "scratch.txt").write_text("notes")
        ticket = self.own_ticket()
        before = self.repo.head()

        task = self.review(_Forge(head), ticket)

        self.assert_held(task, ticket, head=before, cause=HoldCause.UNCOMMITTED, questions=1)

    def test_an_unpushed_commit_ahead_of_the_forge_head_is_held_and_asks_the_owner_once(self) -> None:
        ticket = self.own_ticket()
        ahead = self.repo.commit_locally("local.txt")

        task = self.review(_Forge(self.repo.base), ticket)

        self.assert_held(task, ticket, head=ahead, cause=HoldCause.UNPUSHED, questions=1)

    def test_a_checkout_diverged_from_the_forge_head_is_held_and_asks_the_owner_once(self) -> None:
        head = self.repo.publish("b.txt")
        ticket = self.own_ticket()
        diverged = self.repo.commit_locally("local.txt")

        task = self.review(_Forge(head), ticket)

        self.assert_held(task, ticket, head=diverged, cause=HoldCause.UNPUSHED, questions=1)

    def test_a_repeat_hold_on_the_same_state_does_not_ask_again(self) -> None:
        head = self.repo.publish("b.txt")
        (self.repo.checkout / "a.txt").write_text("edited")
        ticket = self.own_ticket()
        before = self.repo.head()

        self.review(_Forge(head), ticket)
        task = self.review(_Forge(head), ticket)

        self.assert_held(task, ticket, head=before, cause=HoldCause.UNCOMMITTED, questions=1)

    def test_a_changed_state_asks_again(self) -> None:
        head = self.repo.publish("b.txt")
        (self.repo.checkout / "a.txt").write_text("edited")
        ticket = self.own_ticket()
        before = self.repo.head()
        self.review(_Forge(head), ticket)
        (self.repo.checkout / "a.txt").write_text("edited again")

        task = self.review(_Forge(head), ticket)

        self.assert_held(task, ticket, head=before, cause=HoldCause.UNCOMMITTED, questions=2)

    def test_a_checkout_of_another_repository_is_held_without_a_question(self) -> None:
        head = self.repo.publish("b.txt")
        self.repo.point_origin_at("x/y")
        ticket = self.own_ticket()
        before = self.repo.head()

        task = self.review(_Forge(head), ticket)

        self.assert_held(task, ticket, head=before, cause=HoldCause.OTHER_ORIGIN, questions=0)

    def test_a_ticket_with_no_checkout_is_held_without_a_question(self) -> None:
        ticket = TicketFactory.create(role=Ticket.Role.AUTHOR, issue_url=_PR_URL)

        task = self.review(_Forge(self.repo.base), ticket)

        self.assert_held(task, ticket, head=self.repo.head(), cause=HoldCause.NO_CHECKOUT, questions=0)


class TestAnUnknownInputHoldsRatherThanAdmitting(_OwnPrReviewCase):
    harness_must_not_open = True

    def assert_held_at_a(self, task: Task, ticket: Ticket, cause: HoldCause) -> None:
        task.refresh_from_db()
        assert self.spawned == []
        assert self.repo.head() == self.repo.base
        assert self.reviewed_sha(ticket) == ""
        assert task.status == Task.Status.PENDING
        assert cause in task.attempts.order_by("-pk").first().error
        assert DeferredQuestion.objects.count() == 0

    def hold_for(self, cause: HoldCause, *, table: ProcessTable = _READABLE_TABLE, **forge: str) -> None:
        ticket = self.own_ticket()
        reads = _Forge(**{"head": self.repo.publish("b.txt"), **forge})
        self.assert_held_at_a(self.review(reads, ticket, table=table), ticket, cause)

    def test_failed_required_checks_at_the_forge_head(self) -> None:
        self.hold_for(HoldCause.CHECKS, checks="failed")

    def test_unreadable_required_checks_at_the_forge_head(self) -> None:
        self.hold_for(HoldCause.CHECKS, checks="unreadable")

    def test_an_unreadable_forge_head(self) -> None:
        self.hold_for(HoldCause.HEAD_UNREADABLE, head="")

    def test_an_unreadable_pull_request_state(self) -> None:
        self.hold_for(HoldCause.STATE_UNKNOWN, state="")

    def test_an_unreadable_process_table(self) -> None:
        self.hold_for(HoldCause.NO_PROCESS_EVIDENCE, table=_UNREADABLE_TABLE)

    def test_a_fetched_ref_that_is_not_the_head_the_forge_named(self) -> None:
        self.hold_for(HoldCause.FETCHED_OTHER, head="f" * 40)


class TestOtherRoutesAreUntouched(_OwnPrReviewCase):
    def test_a_reviewer_role_ticket_is_not_probed(self) -> None:
        ticket = TicketFactory.create(
            role=Ticket.Role.REVIEWER, issue_url=_PR_URL, extra={"reviewed_sha": self.repo.base}
        )

        self.review(_Forge(self.repo.publish("b.txt")), ticket)

        assert self.spawned
        self.for_ref.assert_not_called()
        self.live_head.assert_not_called()

    def test_an_auto_review_dispatch_task_is_not_probed(self) -> None:
        ticket = self.own_ticket(extra={"reviewed_sha": self.repo.base})
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="reviewing")
        AutoReviewDispatch.objects.create(slug=_SLUG, pr_id=_PR_ID, head_sha=self.repo.base, pr_url=_PR_URL, task=task)

        self.review(_Forge(self.repo.publish("b.txt")), ticket, task=task)

        self.for_ref.assert_not_called()

    def test_a_merged_pull_request_keeps_the_recordability_refusal(self) -> None:
        head = self.repo.publish("b.txt")

        task = self.review(_Forge(head, state="MERGED"))

        assert self.spawned == []
        assert task.status == Task.Status.FAILED
        assert task.attempts.order_by("-pk").first().error.startswith(REVIEW_UNRECORDABLE_PREFIX)
        assert self.repo.head() == self.repo.base
        self.live_head.assert_not_called()

    def test_another_phase_is_not_probed(self) -> None:
        forge, ticket = _Forge(self.repo.publish("b.txt")), self.own_ticket()
        for phase in ("requesting_review", "codex_reviewing"):
            with self.subTest(phase=phase):
                self.review(forge, ticket, phase=phase)

                self.for_ref.assert_not_called()
