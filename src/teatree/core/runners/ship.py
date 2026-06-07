import logging
import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, cast

from teatree.config import load_config
from teatree.core.backend_factory import code_host_for_repo_from_overlay
from teatree.core.backend_protocols import BackendResolutionError, PullRequestSpec
from teatree.core.branch_currency import sha_conflicts_with_target
from teatree.core.close_trailer_scanner import apply_publish_gate
from teatree.core.gates.open_questions_gate import warn_if_open_questions_missing
from teatree.core.overlay_loader import get_overlay
from teatree.core.runners.base import RunnerBase, RunnerResult
from teatree.utils import git

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend
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


def get_overlay_publish_gates() -> list[str]:
    """Return ``ban_close_trailers_on_namespaces`` from ``~/.teatree.toml``.

    Empty list when the setting is absent. Read fresh on each ``pr create``
    so a user edit to the config takes effect on the next invocation
    without restarting the process.
    """
    return list(load_config().user.ban_close_trailers_on_namespaces)


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


def resolve_and_reconcile_branch(ticket: "Ticket", worktree: "Worktree", repo_path: str) -> str:
    """Return the worktree's actual git branch, reconciling the DB to it.

    #1519: ``Worktree.branch`` is minted as ``<N>-ticket`` and the agent
    renames the real git branch to ``<N>-<type>-<desc>``. Every branch-range
    consumer must read what exists, so read ``git rev-parse --abbrev-ref HEAD``
    in the worktree dir and adopt it — but only when it is a real branch that
    belongs to this ticket. A detached ``HEAD`` or an unrelated branch (not
    prefixed ``<ticket_id>-``) falls back to the recorded branch and logs a
    WARNING, never silently adopting an unrelated ref.

    On a genuine drift the recorded ``Worktree.branch`` (and the ticket-level
    ``extra['branch']`` when it matched the old name) are updated to the current
    branch so every later reader — the pre-push gates (#1587), the ship
    executor, the merge path, ``_recorded_url_for_branch``, the provisioner —
    sees the same branch. This is the single reconcile chokepoint: callers run
    it BEFORE reading ``worktree.branch`` so the stale ``<N>-ticket`` ref can no
    longer reach a ``git`` range query that fails fail-soft (#1587). Idempotent:
    once reconciled, a second call resolves the same current branch with no
    further write.
    """
    recorded = worktree.branch
    current = git.current_branch(repo=repo_path)
    prefix = f"{ticket.ticket_number}-"
    if not current or current == "HEAD" or not current.startswith(prefix):
        if current and current != recorded:
            logger.warning(
                "Ship branch resolution for ticket %s: worktree at %s is on %r "
                "(detached or not prefixed %r) — falling back to recorded branch %r",
                ticket.ticket_number,
                repo_path,
                current,
                prefix,
                recorded,
            )
        return recorded
    if current != recorded:
        logger.info(
            "Ship reconciling ticket %s worktree branch %r → %r (renamed in the worktree)",
            ticket.ticket_number,
            recorded,
            current,
        )
        worktree.branch = current
        worktree.save(update_fields=["branch"])
        extra = ticket.extra or {}
        if extra.get("branch") == recorded:
            ticket.merge_extra(set_keys={"branch": current})
    return current


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

        worktree = resolve_ship_worktree(ticket, extra)
        if worktree is None:
            return RunnerResult(ok=False, detail="no worktree on ticket")

        repo_path = (worktree.extra or {}).get("worktree_path", "") or worktree.repo_path

        host = self._resolve_host(repo_path)
        if isinstance(host, RunnerResult):
            return host

        # #1519: the DB-recorded branch is minted as ``<N>-ticket`` at
        # ``workspace ticket`` time; agents then rename the git branch to the
        # ``<N>-<type>-<desc>`` convention. Ship the worktree's ACTUAL current
        # git branch — that is what exists in the worktree — and reconcile the
        # stale DB rows so the worktree↔branch mapping stops desyncing.
        # (Minting the convention name upfront — option 1 — is a separate
        # design decision deliberately left out of this fix.)
        branch = resolve_and_reconcile_branch(ticket, worktree, repo_path)

        # #1263: short-circuit only when THIS branch already has a PR.
        # The legacy truthiness check fired on any prior ``pr_urls`` entry,
        # so on a reused-ticket multi-workstream flow a stale URL from an
        # earlier workstream silently advanced the FSM without pushing or
        # opening a PR for the current branch. ``pr_url_by_branch`` is the
        # per-branch index; fall back to ``pr_urls[-1]`` only when no
        # ``ship_invoking_branch`` hint is recorded (single-PR async-worker
        # path with no multi-workstream context).
        recorded_url = self._recorded_url_for_branch(extra, branch)
        if recorded_url:
            return RunnerResult(ok=True, detail=recorded_url)

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
        # durable backlog entry only when the branch now *conflicts*
        # with target — a behind-but-mergeable branch pushes fine.
        currency_error = self._check_branch_currency(ticket, extra, repo_path, branch)
        if currency_error is not None:
            return RunnerResult(ok=False, detail=currency_error)

        git.push(repo=repo_path, remote="origin", branch=branch)
        spec = self._build_pr_spec(ticket, host, repo_path, branch, extra)
        return self._open_pr_and_record(ticket, extra, host, spec, branch)

    @staticmethod
    def _resolve_host(repo_path: str) -> "CodeHostBackend | RunnerResult":
        """Resolve the forge from *repo_path*'s actual origin host (#2025).

        Token-presence precedence picked GitHub for a GitLab-hosted repo on
        an overlay carrying both PATs, so ship ran ``gh`` against a GitLab
        remote. Deriving the forge from the repo's origin fixes that; a
        forge with no configured credentials surfaces as a structured
        failure here, before any raw ``gh``/``glab`` GraphQL error.
        """
        try:
            host = code_host_for_repo_from_overlay(repo_path)
        except BackendResolutionError as exc:
            return RunnerResult(ok=False, detail=str(exc))
        if host is None:
            return RunnerResult(ok=False, detail="no code host configured")
        return host

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
        self._record_pr_url(ticket, extra, url, branch)
        logger.info("Ship executor pushed %s and opened PR %s", branch, url)
        return RunnerResult(ok=True, detail=url)

    @staticmethod
    def _check_branch_currency(
        ticket: "Ticket",
        extra: "TicketExtra",
        repo_path: str,
        branch: str,
    ) -> str | None:
        """#940 defense-in-depth: refuse to push only on a real conflict.

        The ``pr create`` gate ran auto-merge before the async-worker
        window opened. If ``origin/<target>`` advanced again since AND
        the branch now *conflicts* with it, the loop escalates via a
        durable backlog entry (the worker cannot re-derive consent to
        mutate the working tree to resolve conflicts). A branch that is
        merely behind but conflict-free pushes fine — being behind is
        not a push blocker. ``sha_conflicts_with_target`` predicts the
        merge via ``git merge-tree`` without mutating the worktree, so
        this stays a non-mutating defense gate.
        """
        explicit = str(extra.get("target_branch") or "").strip()
        if explicit:
            target = explicit if "/" in explicit else f"origin/{explicit}"
        else:
            try:
                target = f"origin/{git.default_branch(repo=repo_path)}"
            except (RuntimeError, ValueError):
                return None
        conflict = sha_conflicts_with_target(repo_path, branch, target)
        if conflict is None:
            return None
        # Record on the ticket so the orchestrator's backlog scanner
        # can pick this up — durable signal, not an ephemeral log.
        ticket.merge_extra(
            set_keys={
                "ship_branch_currency_blocker": {
                    "branch": branch,
                    "target": target,
                    "behind": conflict.behind_count,
                    "conflicting_paths": list(conflict.conflicting_paths),
                }
            },
        )
        paths_str = ", ".join(conflict.conflicting_paths) if conflict.conflicting_paths else "(see git status)"
        return (
            f"refusing to push: {branch!r} conflicts with {target} in: {paths_str} — "
            f"merge {target} into the branch, resolve the conflicts, then re-run `pr create`."
        )

    @staticmethod
    def _clear_invoking_branch(ticket: "Ticket", extra: "TicketExtra") -> None:
        if "ship_invoking_branch" in extra:
            # #800 N3: canonical locked RMW (was an unlocked extra save).
            ticket.merge_extra(pop_keys=["ship_invoking_branch"])

    @staticmethod
    def _recorded_url_for_branch(extra: "TicketExtra", branch: str) -> str:
        """The PR URL recorded for ``branch``, or ``""`` if none.

        #1263: short-circuit only when the *current* branch already has a
        PR. ``pr_url_by_branch`` is the per-branch index populated by
        ``_record_pr_url`` on each successful ship; it tells us reliably
        whether the invoking branch's PR exists. The legacy single-PR
        fallback (``pr_urls[-1]`` when no ``ship_invoking_branch`` hint
        is set) preserves async-worker idempotency for tickets that
        pre-date the per-branch index.
        """
        by_branch = extra.get("pr_url_by_branch")
        if isinstance(by_branch, Mapping):
            recorded = by_branch.get(branch)
            if isinstance(recorded, str) and recorded:
                return recorded
        invoking = str(extra.get("ship_invoking_branch") or "")
        if invoking:
            return ""
        legacy_urls = list(extra.get("pr_urls") or [])
        return legacy_urls[-1] if legacy_urls else ""

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
        overlay = get_overlay()
        # PRODUCE the title from structured data unless the user pinned one
        # via --title. The overlay default returns the subject unchanged.
        generated = overlay.metadata.build_pr_title(
            branch=branch,
            subject=subject,
            body=body or "",
            issue_url=ticket.issue_url or "",
        )
        title = title_override or generated or f"Resolve {ticket.issue_url}"
        close_ticket = should_close_ticket(
            extra,
            setting_enabled=overlay.config.mr_close_ticket,
        )
        # Build the description's FIRST LINE from the (sanitized) title, not the
        # raw subject — otherwise a canonical generated title diverges from the
        # raw-subject first line, the exact title/description divergence that
        # blocks the release-notes pipeline.
        sanitized_title = sanitize_close_keywords(title, close_ticket=close_ticket)
        title = sanitized_title
        sanitized_body = sanitize_close_keywords(body, close_ticket=close_ticket) if body else ""
        description = f"{sanitized_title}\n\n{sanitized_body}" if sanitized_body else sanitized_title
        description = apply_publish_gate(
            description,
            repo=repo_path,
            patterns=get_overlay_publish_gates(),
        )
        warn_if_open_questions_missing(description)
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
    def _record_pr_url(ticket: "Ticket", extra: "TicketExtra", url: str, branch: str) -> None:
        urls = list(extra.get("pr_urls") or [])
        if url and url not in urls:
            urls.append(url)
        # #1263: also index by branch so a later workstream on the same
        # ticket can tell whether its own PR exists, without relying on
        # the truthiness of the shared ``pr_urls`` list.
        by_branch_raw = extra.get("pr_url_by_branch")
        by_branch: dict[str, str] = (
            {str(k): str(v) for k, v in by_branch_raw.items()} if isinstance(by_branch_raw, Mapping) else {}
        )
        if url and branch:
            by_branch[branch] = url
        # #800 N3: canonical locked RMW — a concurrent visual_qa /
        # reviewed_sha writer no longer clobbers pr_urls.
        ticket.merge_extra(
            set_keys={"pr_urls": urls, "pr_url_by_branch": by_branch},
            pop_keys=["pr_title_override", "ship_invoking_branch"],
        )
