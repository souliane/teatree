"""A review of the loop's own pull request is dispatched at the head its checkout and the forge share (#5190).

Real git under a temporary origin and a real ticket checkout; only the forge reads, the process table and
the SDK child are doubles. The SDK double RAISES when a held dispatch opens it, so a guard that holds one
instruction too late fails with the spawn itself rather than with a soft assertion.
"""

import shutil
import tempfile
from collections.abc import Callable
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

    def point_origin_at(self, slug: str, *, host: str = "github.com") -> None:
        url = f"https://{host}/{slug}.git"
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
    explode: bool = False
    head_reads: tuple[str, ...] | None = None

    def query(self) -> SimpleNamespace:
        return SimpleNamespace(pr_merge_state=self.merge_state, required_checks_status=lambda: self.checks)

    def merge_state(self) -> PrMergeState:
        if self.explode:
            raise ConnectionError(self.state)
        return PrMergeState(state=self.state, merge_commit_oid="")

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
        table: ProcessTable | Callable[..., ProcessTable] = _READABLE_TABLE,
        phase: str = "reviewing",
        task: Task | None = None,
    ) -> Task:
        with ExitStack() as stack:
            self.for_ref = stack.enter_context(patch.object(guard.CodeHostQuery, "for_ref", return_value=forge.query()))
            if forge.head_reads is None:
                self.live_head = stack.enter_context(
                    patch.object(guard, "live_head_at", return_value=forge.head_read())
                )
            else:
                reads = (LiveHeadRead(sha=head, unreadable=not head) for head in forge.head_reads)
                self.live_head = stack.enter_context(patch.object(guard, "live_head_at", side_effect=reads))
            reads_table = table if callable(table) else lambda **_: table
            stack.enter_context(patch.object(guard, "read_process_table", side_effect=reads_table))
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

    def test_a_stale_ticket_object_restamps_the_database_head(self) -> None:
        ticket = self.own_ticket(extra={"reviewed_sha": self.repo.base})
        Ticket.objects.filter(pk=ticket.pk).update(extra={})

        self.review(_Forge(self.repo.base), ticket)

        assert self.spawned
        assert self.reviewed_sha(ticket) == self.repo.base


class TestAnUnsafeCheckoutIsHeldNotMoved(_OwnPrReviewCase):
    harness_must_not_open = True

    def assert_held(self, task: Task, ticket: Ticket, *, head: str, cause: str, questions: int) -> None:
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

        task = self.review(_Forge(head), ticket)

        self.assert_held(task, ticket, head=self.repo.base, cause=HoldCause.UNCOMMITTED, questions=1)

    def test_an_equal_head_with_uncommitted_work_is_held_before_review(self) -> None:
        (self.repo.checkout / "a.txt").write_text("edited")
        ticket = self.own_ticket()

        task = self.review(_Forge(self.repo.base), ticket)

        self.assert_held(task, ticket, head=self.repo.base, cause=HoldCause.UNCOMMITTED, questions=1)

    def test_an_equal_head_with_failed_checks_is_held_before_review(self) -> None:
        ticket = self.own_ticket()

        task = self.review(_Forge(self.repo.base, checks="failed"), ticket)

        self.assert_held(task, ticket, head=self.repo.base, cause=HoldCause.CHECKS, questions=0)

    def test_an_equal_head_with_unreadable_checks_is_held_before_review(self) -> None:
        ticket = self.own_ticket()

        task = self.review(_Forge(self.repo.base, checks="unreadable"), ticket)

        self.assert_held(task, ticket, head=self.repo.base, cause=HoldCause.CHECKS, questions=0)

    def test_a_ticket_stamp_that_does_not_persist_is_held(self) -> None:
        ticket = self.own_ticket()

        with patch.object(Ticket, "rearm_review_at", return_value=None):
            task = self.review(_Forge(self.repo.base), ticket)

        self.assert_held(
            task, ticket, head=self.repo.base, cause="the ticket did not record the forge head", questions=0
        )

    def test_a_reported_merge_success_without_a_head_move_is_held(self) -> None:
        head = self.repo.publish("b.txt")
        ticket = self.own_ticket()
        real_git = guard.git.run_strict

        def missing_merge(*, repo: str, args: list[str]) -> str:
            if args[:2] == ["merge", "--ff-only"]:
                return ""
            return real_git(repo=repo, args=args)

        with patch.object(guard.git, "run_strict", side_effect=missing_merge):
            task = self.review(_Forge(head), ticket)

        self.assert_held(task, ticket, head=self.repo.base, cause=HoldCause.NOT_LANDED, questions=0)

    def test_a_forge_head_that_moves_during_alignment_is_held(self) -> None:
        head = self.repo.publish("b.txt")
        ticket = self.own_ticket()

        task = self.review(_Forge(head, head_reads=(head, "c" * 40)), ticket)

        self.assert_held(task, ticket, head=head, cause="the forge head moved during alignment", questions=0)

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

    def test_the_owner_is_asked_once_per_distinct_state(self) -> None:
        head = self.repo.publish("b.txt")
        (self.repo.checkout / "a.txt").write_text("edited")
        ticket = self.own_ticket()
        before = self.repo.head()

        self.review(_Forge(head), ticket)
        repeated = self.review(_Forge(head), ticket)
        self.assert_held(repeated, ticket, head=before, cause=HoldCause.UNCOMMITTED, questions=1)

        (self.repo.checkout / "a.txt").write_text("edited again")
        changed = self.review(_Forge(head), ticket)
        self.assert_held(changed, ticket, head=before, cause=HoldCause.UNCOMMITTED, questions=2)

    def test_a_checkout_on_another_branch_is_held_without_a_question(self) -> None:
        head = self.repo.publish("b.txt")
        run_git(self.repo.checkout, "checkout", "-q", "-b", "other")
        ticket = self.own_ticket()

        task = self.review(_Forge(head), ticket)

        self.assert_held(task, ticket, head=self.repo.base, cause=HoldCause.WRONG_BRANCH, questions=0)

    def test_a_pull_request_head_the_remote_does_not_serve_is_held_without_a_question(self) -> None:
        head = self.repo.publish("b.txt")
        run_git(self.repo.origin, "update-ref", "-d", f"refs/pull/{_PR_ID}/head")
        ticket = self.own_ticket()

        task = self.review(_Forge(head), ticket)

        self.assert_held(task, ticket, head=self.repo.base, cause=HoldCause.FETCH_FAILED, questions=0)

    def test_a_live_process_in_the_checkout_holds_the_advance(self) -> None:
        head = self.repo.publish("b.txt")
        ticket = self.own_ticket()
        busy = ProcessTable(frozenset({self.repo.checkout}), "test-table")

        task = self.review(_Forge(head), ticket, table=busy)

        self.assert_held(task, ticket, head=self.repo.base, cause=HoldCause.BUSY, questions=0)

    def test_a_checkout_that_changes_while_the_head_is_fetched_is_held(self) -> None:
        head = self.repo.publish("b.txt")
        ticket = self.own_ticket()

        def racing_editor(**_: object) -> ProcessTable:
            (self.repo.checkout / "racer.txt").write_text("late edit")
            return _READABLE_TABLE

        task = self.review(_Forge(head), ticket, table=racing_editor)

        self.assert_held(task, ticket, head=self.repo.base, cause=HoldCause.CHANGED, questions=0)

    def test_a_checkout_whose_head_cannot_be_read_is_held_without_a_question(self) -> None:
        ticket = self.own_ticket()
        git_head = self.repo.checkout / ".git" / "HEAD"
        git_head.write_text("ref: refs/heads/unborn\n")

        task = self.review(_Forge(self.repo.base), ticket)

        git_head.write_text(f"ref: refs/heads/{_BRANCH}\n")
        self.assert_held(task, ticket, head=self.repo.base, cause=HoldCause.UNREADABLE_CHECKOUT, questions=0)

    def test_a_checkout_of_another_repository_is_held_without_a_question(self) -> None:
        head = self.repo.publish("b.txt")
        self.repo.point_origin_at("x/y")
        ticket = self.own_ticket()
        before = self.repo.head()

        task = self.review(_Forge(head), ticket)

        self.assert_held(task, ticket, head=before, cause=HoldCause.OTHER_ORIGIN, questions=0)

    def test_a_same_slug_on_another_forge_is_held_without_a_question(self) -> None:
        head = self.repo.publish("b.txt")
        self.repo.point_origin_at(_SLUG, host="gitlab.com")
        ticket = self.own_ticket()

        task = self.review(_Forge(head), ticket)

        self.assert_held(task, ticket, head=self.repo.base, cause=HoldCause.OTHER_ORIGIN, questions=0)

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

    def hold_for(self, cause: HoldCause, *, table: ProcessTable = _READABLE_TABLE, **forge: object) -> None:
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

    def test_a_forge_read_that_raises(self) -> None:
        self.hold_for(HoldCause.FORGE_UNREADABLE, explode=True)

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
