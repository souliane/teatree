import logging
import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, cast

from teatree.backends.protocols import PullRequestSpec
from teatree.core.backend_factory import code_host_from_overlay
from teatree.core.branch_currency import branch_behind_target
from teatree.core.overlay_loader import get_overlay
from teatree.core.runners.base import RunnerBase, RunnerResult
from teatree.utils import git

if TYPE_CHECKING:
    from teatree.backends.protocols import CodeHostBackend
    from teatree.core.models.ticket import Ticket
    from teatree.core.models.types import TicketExtra
    from teatree.core.models.worktree import Worktree

logger = logging.getLogger(__name__)

# Single source of truth for close-keyword detection, shared with the
# pre-push gate (``_close_keyword_gate.py``) so the gate and the auto-rewrite
# stay in lockstep (#1090). The ``(?::\s*|\s+)`` separator matches the colon
# form GitLab's default ``issue_closing_pattern`` accepts — ``Closes: #N``
# auto-closes the issue on merge — while leaving ``Closes : #N`` (a space
# BEFORE the colon, which GitLab's real ``(:?) +`` grammar does not auto-close)
# unmatched. The verb set is the past-tense-inclusive superset
# (``close[sd]?|fix(?:e[sd])?|resolve[sd]?``) GitHub/GitLab both recognise.
CLOSE_KEYWORD_RE = re.compile(
    r"\b(?P<kw>close[sd]?|fix(?:e[sd])?|resolve[sd]?)(?::\s*|\s+)"
    r"(?P<ref>(?:[\w./-]+)?#\d+|https?://\S+/issues/\d+)",
    re.IGNORECASE,
)


def sanitize_close_keywords(description: str, *, close_ticket: bool) -> str:
    """Replace ``Closes/Fixes/Resolves #N`` with ``Relates to`` when not closing."""
    if close_ticket:
        return description
    return CLOSE_KEYWORD_RE.sub(r"Relates to \g<ref>", description)


def should_close_ticket(extra: Mapping[str, object] | None, *, setting_enabled: bool) -> bool:
    """Resolve the effective close-on-merge disposition for a PR.

    The default is **close-on-merge**: a merged PR should systematically
    close its referenced issue when the overlay's auto-close setting is
    enabled. Suppression is the exception, applied only on an explicit
    "more PRs are coming for this ticket/issue" signal — a declared
    partial PR or an umbrella issue with remaining tracked scope, recorded
    as ``extra['more_prs_coming']``. This preserves the umbrella/partial
    protection (``feedback_partial_pr_never_closes_umbrella_issue``)
    without defeating the setting for standalone single-target bug PRs.

    Returns ``True`` when ``Closes/Fixes #N`` keywords must be kept so the
    platform auto-closes the issue on merge; ``False`` when they must be
    rewritten to ``Relates to`` (setting disabled, or an explicit
    follow-up opt-out is set).
    """
    if not setting_enabled:
        return False
    more_prs_coming = bool(extra and extra.get("more_prs_coming"))
    return not more_prs_coming


def overlay_pr_labels() -> list[str]:
    raw = get_overlay().config.pr_auto_labels
    if isinstance(raw, str):
        values: Iterable[str] = raw.split(",")
    elif isinstance(raw, Iterable):
        values = [str(value) for value in raw]
    else:
        return []
    return [value.strip() for value in values if value.strip()]


def resolve_ship_worktree(ticket: "Ticket", extra: "TicketExtra") -> "Worktree | None":
    """The worktree to act on — the INVOKING branch's row, not the earliest.

    #776: ``worktrees.first()`` returns the earliest (often
    already-merged) row, so a reused ticket spanning N workstreams acted
    on a stale branch. ``pr create`` records the invoking worktree's
    current git branch on ``extra['ship_invoking_branch']``; prefer the
    matching row. Fall back to ``first()`` only when no invoking branch
    is recorded (the async-worker path that has no CLI cwd context) —
    legacy behaviour, single-PR tickets unaffected. Public so the
    pre-push visual-QA gate resolves the same worktree as the ship.
    """
    invoking = str(extra.get("ship_invoking_branch") or "")
    if invoking:
        matched = ticket.worktrees.filter(branch=invoking).first()  # ty: ignore[unresolved-attribute]
        if matched is not None:
            return matched
    return ticket.worktrees.first()  # ty: ignore[unresolved-attribute]


class ShipExecutor(RunnerBase):
    """Push the worktree branch and open the pull request.

    Runs inside ``execute_ship`` after the FSM advances to ``SHIPPED``. The
    worker calls ``request_review()`` on success to advance to ``IN_REVIEW``.
    """

    def __init__(self, ticket: "Ticket") -> None:
        self.ticket = ticket

    def run(self) -> RunnerResult:
        ticket = self.ticket
        extra = cast("TicketExtra", ticket.extra or {})
        existing_urls = list(extra.get("pr_urls") or [])
        if existing_urls:
            return RunnerResult(ok=True, detail=existing_urls[-1])

        worktree = resolve_ship_worktree(ticket, extra)
        if worktree is None:
            return RunnerResult(ok=False, detail="no worktree on ticket")

        host = code_host_from_overlay()
        if host is None:
            return RunnerResult(ok=False, detail="no code host configured")

        repo_path = (worktree.extra or {}).get("worktree_path", "") or worktree.repo_path
        branch = worktree.branch

        # #776: a ticket can span multiple PRs (one branch per workstream).
        # Refuse to re-open a PR for a branch already merged into base —
        # that is the stale-row symptom (a junk duplicate of merged work).
        if git.branch_merged(repo=repo_path, branch=branch):
            self._clear_invoking_branch(ticket, extra)
            return RunnerResult(
                ok=False, detail=f"branch {branch!r} is already merged into base — refusing duplicate PR"
            )

        # #940 defense-in-depth: re-check branch currency before
        # pushing. The `pr create` gate already auto-merged the target,
        # but ``execute_ship`` may run in an async worker after a
        # window where ``origin/<target>`` advanced again. Abort with a
        # durable backlog entry rather than push a stale base.
        currency_error = self._check_branch_currency(ticket, extra, repo_path, branch)
        if currency_error is not None:
            return RunnerResult(ok=False, detail=currency_error)

        git.push(repo=repo_path, remote="origin", branch=branch)
        spec = self._build_pr_spec(ticket, host, repo_path, branch, extra)
        return self._open_pr_and_record(ticket, extra, host, spec, branch)

    def _open_pr_and_record(
        self,
        ticket: "Ticket",
        extra: "TicketExtra",
        host: "CodeHostBackend",
        spec: PullRequestSpec,
        branch: str,
    ) -> RunnerResult:
        """Open the PR, verify the URL is present, and record it on the ticket.

        #1222 / #1226 verify-by-re-read: a backend that returns a payload
        without a URL (or with the wrong field name) MUST surface as
        ``ok=False`` — otherwise the FSM advances to SHIPPED with an empty
        ``pr_urls`` entry and downstream gates think no PR exists.
        ``web_url`` is the cross-host canonical key; ``html_url`` is kept
        for raw GitHub API payloads piped through other producers.
        """
        pr = host.create_pr(spec)
        url = str(pr.get("web_url") or pr.get("html_url") or "")
        if not url.startswith(("http://", "https://")):
            return RunnerResult(
                ok=False,
                detail=(f"host.create_pr returned no PR url (got {url!r}; payload keys={sorted(pr.keys())!r})"),
            )
        self._record_pr_url(ticket, extra, url)
        logger.info("Ship executor pushed %s and opened PR %s", branch, url)
        return RunnerResult(ok=True, detail=url)

    @staticmethod
    def _check_branch_currency(
        ticket: "Ticket",
        extra: "TicketExtra",
        repo_path: str,
        branch: str,
    ) -> str | None:
        """#940 defense-in-depth: refuse to push when target advanced again.

        The ``pr create`` gate ran auto-merge before the async-worker
        window opened. If ``origin/<target>`` has advanced again since,
        the loop must escalate via a durable backlog entry (the worker
        cannot re-derive consent to mutate the working tree) rather
        than push a stale base. ``branch_behind_target`` only reports —
        it never merges — so this stays a non-mutating defense gate.
        """
        explicit = str(extra.get("target_branch") or "").strip()
        if explicit:
            target = explicit if "/" in explicit else f"origin/{explicit}"
        else:
            try:
                target = f"origin/{git.default_branch(repo=repo_path)}"
            except (RuntimeError, ValueError):
                return None
        staleness = branch_behind_target(repo_path, branch, target)
        if staleness is None:
            return None
        # Record on the ticket so the orchestrator's backlog scanner
        # can pick this up — durable signal, not an ephemeral log.
        ticket.merge_extra(
            set_keys={
                "ship_branch_currency_blocker": {
                    "branch": branch,
                    "target": target,
                    "behind": staleness.behind_count,
                }
            },
        )
        return (
            f"refusing to push: {branch!r} is {staleness.behind_count} commit(s) behind "
            f"{target} — re-run `pr create` after merging target into the branch."
        )

    @staticmethod
    def _clear_invoking_branch(ticket: "Ticket", extra: "TicketExtra") -> None:
        if "ship_invoking_branch" in extra:
            # #800 N3: canonical locked RMW (was an unlocked extra save).
            ticket.merge_extra(pop_keys=["ship_invoking_branch"])

    @staticmethod
    def _build_pr_spec(
        ticket: "Ticket",
        host: "CodeHostBackend",
        repo_path: str,
        branch: str,
        extra: "TicketExtra",
    ) -> PullRequestSpec:
        title_override = str(extra.get("pr_title_override") or "")
        subject, body = git.last_commit_message(repo=repo_path)
        title = title_override or subject or f"Resolve {ticket.issue_url}"
        raw_description = f"{subject}\n\n{body}" if subject and body else (subject or body)
        close_ticket = should_close_ticket(
            extra,
            setting_enabled=get_overlay().config.mr_close_ticket,
        )
        description = sanitize_close_keywords(raw_description, close_ticket=close_ticket)
        assignee = host.current_user() or git.config_value(key="user.name")
        return PullRequestSpec(
            repo=repo_path,
            branch=branch,
            title=title,
            description=description,
            labels=overlay_pr_labels(),
            assignee=assignee,
        )

    @staticmethod
    def _record_pr_url(ticket: "Ticket", extra: "TicketExtra", url: str) -> None:
        urls = list(extra.get("pr_urls") or [])
        if url and url not in urls:
            urls.append(url)
        # #800 N3: canonical locked RMW — a concurrent visual_qa /
        # reviewed_sha writer no longer clobbers pr_urls.
        ticket.merge_extra(set_keys={"pr_urls": urls}, pop_keys=["pr_title_override", "ship_invoking_branch"])
