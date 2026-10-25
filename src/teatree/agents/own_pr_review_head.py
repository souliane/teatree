"""A review of the loop's own pull request is dispatched at the head its checkout and the forge share (#5190).

The reviewer reads the checkout's diff while its verdict binds to the head stamped on the ticket, so a
branch pushed after the checkout was prepared yields a verdict about code nobody read. Before such a
review opens, this guard makes the checkout and the forge agree on one head and stamps it, or holds the
dispatch with a recorded reason. It moves a checkout only when that checkout is clean and provably behind.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from functools import partial
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from django.db import transaction
from django.utils import timezone

from teatree.agents.usage_window import record_park
from teatree.core.cleanup.process_table import read_process_table
from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.modelkit.forge_readability import REFUSING_CHECK_VERDICTS
from teatree.core.modelkit.gate_verdict import EvidenceUnavailableError, read_or_refuse
from teatree.core.modelkit.phase_tools import ENVELOPE_VERDICT_PHASES
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import DeferredQuestion, Task, TaskAttempt, Worktree
from teatree.core.models.checkout_custody import Custody
from teatree.core.models.ticket_worktree_checks import dispatch_worktree
from teatree.core.models.usage_window_state import REVIEW_HELD_PREFIX
from teatree.core.models.worktree_occupancy import task_holder_id
from teatree.core.review.live_head import live_head_at
from teatree.core.worktree.checkout_state import CheckoutState, checkout_state
from teatree.utils import git
from teatree.utils.git_remote import host_from_remote, slug_from_remote
from teatree.utils.git_run import git_env_non_interactive, run_with_status
from teatree.utils.pr_ref import PrRef
from teatree.utils.run import CommandFailedError, TimeoutExpired
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)

_HOLD_RETRY = timedelta(minutes=30)
_FETCH_TIMEOUT_SECONDS = 120.0
_OPEN_STATES = frozenset({"OPEN", "OPENED"})
_SETTLED_STATES = frozenset({"MERGED", "CLOSED", "LOCKED"})
_QUESTION_MARKER_PREFIX = "review-hold:"
_GIT_FAILURES = (OSError, RuntimeError, ValueError, TimeoutExpired, CommandFailedError)


class HoldCause(StrEnum):
    FORGE_UNREADABLE = "the forge did not answer for"
    STATE_UNKNOWN = "the forge did not say whether the pull request is open"
    HEAD_UNREADABLE = "the forge did not name the pull request head"
    HEAD_MOVED = "the forge head moved during alignment"
    NO_CHECKOUT = "the ticket has no checkout on disk"
    OTHER_ORIGIN = "the ticket's checkout has another origin:"
    WRONG_BRANCH = "the ticket's checkout is not on the ticket branch, it is on"
    UNCOMMITTED = "the ticket's checkout holds uncommitted work"
    UNPUSHED = "the ticket's checkout holds commits the pull request does not"
    FETCH_FAILED = "the pull request head could not be fetched:"
    FETCHED_OTHER = "the fetched head is not the head the forge named"
    NOT_COMPARABLE = "the checkout could not be compared with the forge head:"
    CHECKS = "the required checks at the forge head are"
    NO_PROCESS_EVIDENCE = "there is no live-process evidence for the checkout:"
    BUSY = "a live process is working in the checkout"
    CHANGED = "the ticket's checkout changed while the head was fetched"
    NOT_LANDED = "the ticket's checkout did not land on the forge head"
    NOT_STAMPED = "the ticket did not record the forge head"
    UNREADABLE_CHECKOUT = "the checkout could not be read:"


class _ReviewHeldError(Exception):
    """The review must not open yet; ``unsaved_state`` keys the owner's work when that is what blocks it."""

    def __init__(self, cause: HoldCause, detail: str = "", *, unsaved_state: str = "") -> None:
        super().__init__(f"{cause} {detail}".strip())
        self.unsaved_state = unsaved_state


def prepare_own_pr_review_head(task: Task, *, phase: str) -> TaskAttempt | None:
    """A parked attempt when *task*'s own-PR review must wait, else ``None``: the checkout is then at the forge head."""
    ref = _own_pr_ref(task, phase)
    if ref is None:
        return None
    try:
        _OwnPrHead(task, ref).align()
    except _ReviewHeldError as held:
        return _record_hold(task, ref, held)
    return None


def _own_pr_ref(task: Task, phase: str) -> PrRef | None:
    ticket = task.ticket
    if normalize_phase(phase) not in ENVELOPE_VERDICT_PHASES or ticket.role != ticket.Role.AUTHOR:
        return None
    if task.auto_review_dispatches.exists():  # ty: ignore[unresolved-attribute]
        return None
    return pr_ref_from_url(ticket.issue_url)


@dataclass(frozen=True, slots=True)
class _OwnPrHead:
    task: Task
    ref: PrRef

    def align(self) -> None:
        forge = self._read("the pull request", partial(CodeHostQuery.for_ref, self.ref))
        if self._settled(forge):
            return
        head = self._forge_head()
        worktree = self._worktree()
        path = worktree.worktree_path
        before = self._state(path)
        local = self._git(path, "rev-parse", "--verify", "HEAD")
        if local != head and before.branch != worktree.branch:
            raise _ReviewHeldError(HoldCause.WRONG_BRANCH, before.branch)
        if before.dirty:
            raise _ReviewHeldError(HoldCause.UNCOMMITTED, unsaved_state=f"{head}:{before.digest}")
        if local != head:
            self._advance(worktree, forge, head, local, before)
        else:
            self._require_checks(forge)
        if self._forge_head() != head:
            raise _ReviewHeldError(HoldCause.HEAD_MOVED)
        self._stamp(head)

    def _stamp(self, head: str) -> None:
        ticket = self.task.ticket
        ticket.refresh_from_db(fields=["extra"])
        ticket.rearm_review_at(head)
        ticket.refresh_from_db(fields=["extra"])
        if (ticket.extra or {}).get("reviewed_sha") != head:
            raise _ReviewHeldError(HoldCause.NOT_STAMPED)

    def _settled(self, forge: CodeHostQuery) -> bool:
        state = self._read("the pull request state", forge.pr_merge_state).state.upper()
        if state not in _OPEN_STATES | _SETTLED_STATES:
            raise _ReviewHeldError(HoldCause.STATE_UNKNOWN)
        return state in _SETTLED_STATES

    def _read[T](self, what: str, read: Callable[[], T]) -> T:
        try:
            return read_or_refuse(what, read)
        except EvidenceUnavailableError as exc:
            logger.warning("%s of %s#%d unreadable", what, self.ref.slug, self.ref.pr_id, exc_info=True)
            raise _ReviewHeldError(HoldCause.FORGE_UNREADABLE, what) from exc

    def _forge_head(self) -> str:
        read = live_head_at(slug=self.ref.slug, pr_id=self.ref.pr_id, host_kind=self.ref.host_kind)
        if read.unreadable or not read.sha:
            raise _ReviewHeldError(HoldCause.HEAD_UNREADABLE)
        return read.sha

    def _worktree(self) -> Worktree:
        worktree = dispatch_worktree(self.task.ticket)
        if worktree is None:
            raise _ReviewHeldError(HoldCause.NO_CHECKOUT)
        origin_url = git.config_value(repo=worktree.worktree_path, key="remote.origin.url")
        origin = slug_from_remote(origin_url)
        host = host_from_remote(origin_url)
        ticket_host = (urlsplit(self.task.ticket.issue_url).hostname or "").lower()
        if origin.casefold() != self.ref.slug.casefold() or not host or host != ticket_host:
            raise _ReviewHeldError(HoldCause.OTHER_ORIGIN, origin or "none")
        return worktree

    def _advance(self, worktree: Worktree, forge: CodeHostQuery, head: str, local: str, before: CheckoutState) -> None:
        path = worktree.worktree_path
        unsaved = f"{head}:{before.digest}"
        self._fetch_pull_head(path, head)
        if not self._is_behind(path, local, head):
            raise _ReviewHeldError(HoldCause.UNPUSHED, unsaved_state=unsaved)
        self._require_checks(forge)
        self._require_quiet_checkout(path)
        if self._state(path).digest != before.digest:
            raise _ReviewHeldError(HoldCause.CHANGED)
        self._git(path, "merge", "--ff-only", head)
        after = self._state(path)
        if after.dirty or self._git(path, "rev-parse", "--verify", "HEAD") != head:
            raise _ReviewHeldError(HoldCause.NOT_LANDED)
        self._carry_attestation(worktree, before.digest, after.digest)

    def _require_checks(self, forge: CodeHostQuery) -> None:
        checks = self._read("the required checks", forge.required_checks_status)
        if checks in REFUSING_CHECK_VERDICTS:
            raise _ReviewHeldError(HoldCause.CHECKS, checks)

    def _fetch_pull_head(self, path: str, head: str) -> None:
        fetch = partial(
            run_with_status,
            repo=path,
            args=["fetch", "--no-tags", "origin", self.ref.head_ref],
            env=git_env_non_interactive(),
            timeout=_FETCH_TIMEOUT_SECONDS,
        )
        if (returncode := self._guarded(HoldCause.FETCH_FAILED, "git fetch", fetch).returncode) != 0:
            raise _ReviewHeldError(HoldCause.FETCH_FAILED, f"git exited {returncode}")
        if self._git(path, "rev-parse", "--verify", "FETCH_HEAD^{commit}") != head:
            raise _ReviewHeldError(HoldCause.FETCHED_OTHER)

    def _is_behind(self, path: str, local: str, head: str) -> bool:
        compare = partial(run_with_status, repo=path, args=["merge-base", "--is-ancestor", local, head])
        returncode = self._guarded(HoldCause.NOT_COMPARABLE, "git merge-base", compare).returncode
        if returncode not in {0, 1}:
            raise _ReviewHeldError(HoldCause.NOT_COMPARABLE, f"git exited {returncode}")
        return returncode == 0

    @staticmethod
    def _require_quiet_checkout(path: str) -> None:
        table = read_process_table(exclude_ancestors=True)
        if reason := table.eviction_refuse_reason():
            raise _ReviewHeldError(HoldCause.NO_PROCESS_EVIDENCE, reason)
        if table.holds(Path(path)):
            raise _ReviewHeldError(HoldCause.BUSY)

    def _state(self, path: str) -> CheckoutState:
        return self._guarded(
            HoldCause.UNREADABLE_CHECKOUT, "state", partial(checkout_state, Path(path), ticket=self.task.ticket)
        )

    def _git(self, path: str, *args: str) -> str:
        return self._guarded(
            HoldCause.UNREADABLE_CHECKOUT, f"git {args[0]}", partial(git.run_strict, repo=path, args=list(args))
        )

    @staticmethod
    def _guarded[T](cause: HoldCause, what: str, call: Callable[[], T]) -> T:
        try:
            return call()
        except _GIT_FAILURES as exc:
            raise _ReviewHeldError(cause, f"{what} {type(exc).__name__}") from exc

    def _carry_attestation(self, worktree: Worktree, before: str, after: str) -> None:
        worktree.refresh_from_db()
        extra = worktree.get_extra()
        if extra.get("attested_state") != before:
            return
        reanchored = Custody.reanchored(
            after, Custody.from_extra(extra), task_holder_id(self.task), self.task.claimed_by_session
        )
        worktree.merge_extra(set_keys={"attested_state": after, "checkout_custody": reanchored.as_dict()})


def _record_hold(task: Task, ref: PrRef, held: _ReviewHeldError) -> TaskAttempt:
    subject = f"{ref.slug}#{ref.pr_id}"
    reason = f"{REVIEW_HELD_PREFIX}the review of {subject} waits because {held}"
    logger.warning("Holding the review dispatch of task %s: %s", task.pk, reason)
    with transaction.atomic():
        attempt = record_park(task, reason=reason, not_before=timezone.now() + _HOLD_RETRY, summary=reason)
        if held.unsaved_state:
            marker = sha256(f"{subject}:{held.unsaved_state}".encode()).hexdigest()[:40]
            DeferredQuestion.record(
                f"[review-hold {subject}] The review waits because {held}. Push or discard that work and "
                "the review runs by itself at the forge head.",
                task_session=task.session,
                dedupe_marker=f"{_QUESTION_MARKER_PREFIX}{marker}",
            )
    return attempt
