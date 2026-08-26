"""Where a test plan lives: a file in the e2e repo, one per ticket.

A test plan is a reviewable artifact of the suite it describes, so it lives
beside that suite in git rather than as a forge comment:
``test-plans/<repo>-<ticket number>.md``, a sibling of the overlay's declared
e2e directory, inside the ticket's checkout of the e2e repo. Nothing here is
configured — the checkout comes from ``overlay.metadata.get_e2e_config()`` and
both halves of the filename from the ticket, so writer and reader resolve the
same path and a re-run can only ever land on the file it already wrote. A plan
already written under the pre-prefix ``<ticket number>.md`` keeps that name for
the rest of its life, so an in-flight ticket is updated rather than forked.

The record itself is unchanged: the body is the same rendered markdown, with
the same hidden ``t3-e2e-data`` state blob, so :func:`read_plan_state` recovers
what the previous run persisted and the merge in :mod:`.render` overlays this
run's side onto it.
"""

from pathlib import Path
from urllib.parse import urlparse

from teatree.core.management.commands._test_plan.state import (
    PlanState,
    TestPlanValidationError,
    empty_state,
    parse_state_blob,
)
from teatree.core.models import Ticket
from teatree.core.overlay_loader import get_overlay
from teatree.utils.url_slug import slug_from_issue_or_pr_url

PLAN_DIR_NAME = "test-plans"

__all__ = ["PLAN_DIR_NAME", "TestPlanLocationError", "plan_path_for_ticket", "read_plan_state", "write_plan"]


class TestPlanLocationError(TestPlanValidationError):
    """The plan file's path could not be resolved; nothing is written.

    A subclass of :class:`TestPlanValidationError` so a caller's single
    ``except`` arm surfaces an unresolvable location as a non-zero exit, the
    same as a malformed manifest — never as a silently-skipped write.
    """

    __test__ = False  # not a pytest test class (name starts with 'Test')


def plan_path_for_ticket(ticket: Ticket) -> Path:
    """``<e2e-repo checkout>/…/test-plans/<repo>-<ticket number>.md`` for *ticket*.

    An existing pre-prefix ``<ticket number>.md`` wins instead: reading and
    rewriting the plan an in-flight ticket already has beats starting a second
    one whose other environment's evidence is silently gone.

    Raises :class:`TestPlanLocationError` when the overlay declares no e2e
    repo, when the ticket has no worktree for it, or when that worktree has not
    been provisioned on disk.
    """
    e2e_config = get_overlay(ticket.overlay or None).metadata.get_e2e_config()
    project_path = e2e_config.get("project_path", "").strip()
    repo = project_path.rsplit("/", 1)[-1]
    if not repo:
        msg = (
            f"Ticket {ticket} has nowhere to store its test plan: overlay {ticket.overlay!r} declares no e2e repo "
            "(get_e2e_config has no 'project_path')."
        )
        raise TestPlanLocationError(msg)
    plan_dir = _plan_dir(_checkout_for(ticket, repo=repo), e2e_dir=e2e_config.get("e2e_dir", "e2e"))
    # Work-item numbers are allocated per repo, so the number alone cannot say which ticket it names.
    prefixed = plan_dir / f"{_ticket_repo(ticket, fallback=repo)}-{ticket.ticket_number}.md"
    legacy = plan_dir / f"{ticket.ticket_number}.md"
    return legacy if legacy.is_file() and not prefixed.is_file() else prefixed


def read_plan_state(path: Path) -> PlanState:
    """The state persisted in the plan file, or an empty state when it does not exist yet."""
    if not path.is_file():
        return empty_state(ticket="", title="")
    return parse_state_blob(path.read_text(encoding="utf-8"))


def write_plan(path: Path, body: str) -> None:
    """Write *body* to *path*, creating the plan directory on first use."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _checkout_for(ticket: Ticket, *, repo: str) -> Path:
    worktree = ticket.worktrees.filter(repo_path=repo).order_by("-pk").first()  # ty: ignore[unresolved-attribute]
    if worktree is None:
        msg = (
            f"Ticket {ticket} has no worktree for the e2e repo {repo!r}, so its test plan has nowhere to live. "
            f"Add one with `t3 <overlay> workspace ticket {ticket.issue_url or ticket.pk}`."
        )
        raise TestPlanLocationError(msg)
    if not worktree.worktree_path:
        msg = f"The {repo!r} worktree for ticket {ticket} is not provisioned on disk (no worktree_path recorded)."
        raise TestPlanLocationError(msg)
    return Path(worktree.worktree_path)


def _plan_dir(checkout: Path, *, e2e_dir: str) -> Path:
    """The ``test-plans`` directory sitting beside the suite it documents."""
    return (checkout / (e2e_dir or "e2e")).parent / PLAN_DIR_NAME


def _ticket_repo(ticket: Ticket, *, fallback: str) -> str:
    """The repo the ticket's number belongs to, named from the ticket's own URL."""
    slug = slug_from_issue_or_pr_url(urlparse(str(ticket.issue_url or "")).path)
    return slug.rsplit("/", 1)[-1] if slug else fallback
