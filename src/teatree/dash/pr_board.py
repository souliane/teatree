"""Open pull requests grouped by the next actor and action."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import TypedDict, cast
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from django.utils import timezone

from teatree.config import get_effective_settings
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.review_request_post import ReviewRequestPost
from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.overlay_loader import get_overlay
from teatree.core.pr_inventory import PrInventoryEntry, elapsed_label, merged_entries, parse_time, read_inventory
from teatree.core.review.mr_triage import DEFAULT_THRESHOLDS
from teatree.dash.selectors import _last_failure_by_ticket
from teatree.dash.task_actions import EnqueueButton, board_enqueue_buttons

_GROUPS = ("Needs you", "Waiting on colleagues", "Factory working", "Ready to merge or merging", "Blocked")
_ACTIVE_PR_STATES = (PullRequest.State.OPEN, PullRequest.State.REVIEW_REQUESTED, PullRequest.State.APPROVED)
_ACTIVE_TASK_STATES = (Task.Status.PENDING, Task.Status.CLAIMED)
_STALE_INVENTORY_AFTER = timedelta(minutes=30)


class CachedPr(TypedDict, total=False):
    repo: str
    iid: int
    title: str
    state: str
    pipeline_status: str
    approvals: dict[str, int]
    discussions: list[dict[str, str]]
    updated_at: str
    review_requested: bool
    review_permalink: str
    draft: bool


@dataclass(frozen=True, slots=True)
class PrBoardCard:
    dom_id: str
    url: str
    link_url: str
    title: str
    ref: str
    age: str
    created_at: datetime | None
    ci: str
    approvals: str
    threads: str
    last_activity: str
    next_actor: str
    next_action: str
    group: str
    ticket_id: int | None
    draft: bool
    review_link: str
    review_age: str
    review_reasks: int
    review_due: str
    work_agent: str
    work_since: str
    last_result: str
    enqueue_buttons: tuple[EnqueueButton, ...]


@dataclass(frozen=True, slots=True)
class PrBoardGroup:
    name: str
    cards: tuple[PrBoardCard, ...]


@dataclass(frozen=True, slots=True)
class PrBoard:
    groups: tuple[PrBoardGroup, ...]
    total: int
    inventory_warning: str


def _web_link(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    else:
        return value if parsed.scheme in {"http", "https"} and parsed.hostname else ""


@dataclass(frozen=True, slots=True)
class PrObservation:
    entries: dict[str, PrInventoryEntry]
    overlays: set[str]
    verified_urls: set[str]
    warning: str


def _inventory(now: datetime) -> PrObservation:
    try:
        snapshots = read_inventory()
    except (OSError, ValueError) as exc:
        return PrObservation({}, set(), set(), f"PR inventory unavailable: {exc}")
    if not snapshots:
        return PrObservation({}, set(), set(), "PR inventory unavailable; showing recorded pull requests.")
    overlays = {snapshot.overlay for snapshot in snapshots if not snapshot.overlay.startswith("own:")}
    stale = [snapshot for snapshot in snapshots if now - snapshot.observed_at > _STALE_INVENTORY_AFTER]
    stale_urls = {entry.url for snapshot in stale for entry in snapshot.entries}
    entries = merged_entries(snapshots)
    warning = f"PR inventory is stale for {', '.join(sorted(item.overlay for item in stale))}." if stale else ""
    return PrObservation(entries, overlays, entries.keys() - stale_urls, warning)


def _cached_prs(tickets: list[Ticket]) -> tuple[dict[str, Ticket], dict[str, CachedPr]]:
    owners: dict[str, Ticket] = {}
    cache: dict[str, CachedPr] = {}
    for ticket in tickets:
        extra = ticket.extra if isinstance(ticket.extra, dict) else {}
        prs = extra.get("prs")
        if not isinstance(prs, dict):
            continue
        for url, raw in prs.items():
            if isinstance(url, str) and isinstance(raw, dict):
                owners[url] = ticket
                cache[url] = cast("CachedPr", raw)
    return owners, cache


def _approval_text(raw: CachedPr) -> str:
    approvals = raw.get("approvals")
    if not isinstance(approvals, dict):
        return "unknown"
    count = approvals.get("count")
    required = approvals.get("required")
    return f"{count}/{required}" if isinstance(count, int) and isinstance(required, int) else "unknown"


def _thread_text(raw: CachedPr) -> str:
    discussions = raw.get("discussions")
    if not isinstance(discussions, list):
        return "unknown"
    unresolved = sum(
        item.get("status") in {"waiting_reviewer", "needs_reply"} for item in discussions if isinstance(item, dict)
    )
    return f"{unresolved} unresolved"


def _owner_questions(urls: set[str], ticket_ids: set[int]) -> tuple[dict[str, str], dict[int, str]]:
    by_url: dict[str, str] = {}
    by_ticket: dict[int, str] = {}
    questions = DeferredQuestion.objects.filter(
        answered_at__isnull=True,
        dismissed_at__isnull=True,
        audience=DeferredQuestion.Audience.OWNER_QUESTION,
    ).select_related("parked_task", "task_session")
    for question in questions:
        evidence = question.evidence if isinstance(question.evidence, dict) else {}
        url = evidence.get("pr_url")
        ticket_id = evidence.get("ticket_id")
        if isinstance(url, str) and url in urls:
            by_url[url] = question.question
        if isinstance(ticket_id, int) and ticket_id in ticket_ids:
            by_ticket[ticket_id] = question.question
        parked_task = question.parked_task if question.parked_task_id else None
        task_session = question.task_session if question.task_session_id else None
        linked_ticket_id = (
            parked_task.ticket_id if parked_task is not None else task_session.ticket_id if task_session else None
        )
        if linked_ticket_id in ticket_ids:
            by_ticket[linked_ticket_id] = question.question
    return by_url, by_ticket


@dataclass(frozen=True, slots=True)
class PrActionFacts:
    entry: PrInventoryEntry | None
    row: PullRequest | None
    task: Task | None
    failure: str
    review_requested: bool
    question: str
    ci: str
    approvals_met: bool | None
    approvals: str
    threads: str
    verified: bool


@dataclass(slots=True)
class PrBoardSources:
    now: datetime
    urls: set[str]
    observed: dict[str, PrInventoryEntry]
    verified_urls: set[str]
    rows_by_url: dict[str, PullRequest]
    owners: dict[str, Ticket]
    cache: dict[str, CachedPr]
    tickets_by_issue_url: dict[str, Ticket]
    tasks: dict[int, Task]
    queued: dict[int, dict[str, int]]
    failures: dict[int, tuple[str, str]]
    review_posts: dict[str, ReviewRequestPost]
    attempts: dict[int, TaskAttempt]
    questions_by_url: dict[str, str]
    questions_by_ticket: dict[int, str]
    warning: str


def _blocked_reason(facts: PrActionFacts) -> str:
    if facts.failure:
        return f"Fix failed task: {facts.failure[:160]}"
    if facts.ci.lower() in {"red", "failed", "failure", "error", "canceled", "cancelled"}:
        return "Fix red CI"
    if facts.threads not in {"unknown", "0 unresolved"}:
        return f"Resolve {facts.threads} thread(s)"
    if facts.entry is not None and facts.entry.decision in {
        "flag_conflict",
        "needs_branch_update",
        "required_checks_indeterminate",
    }:
        return facts.entry.next_task
    return ""


def _authorization_action(entry: PrInventoryEntry) -> tuple[str, str, str]:
    if entry.reason == "mergeable_awaiting_review":
        return "Needs you", "You", entry.next_task
    if entry.next_task == "Update PR branch against main":
        return "Blocked", "Factory", entry.next_task
    return "Waiting on colleagues", "Reviewer", entry.next_task


def _next_action(facts: PrActionFacts) -> tuple[str, str, str]:
    if facts.question:
        outcome = ("Needs you", "You", facts.question)
    elif facts.task is not None:
        outcome = ("Factory working", "Factory", f"{facts.task.phase} ({facts.task.status})")
    elif blocked := _blocked_reason(facts):
        outcome = ("Blocked", "Factory", blocked)
    elif facts.entry is not None and facts.entry.reason in {"no_clear_for_head", "mergeable_awaiting_review"}:
        outcome = _authorization_action(facts.entry)
    elif facts.approvals_met is False:
        outcome = ("Waiting on colleagues", "Reviewer", f"Await approvals ({facts.approvals})")
    elif facts.review_requested or (facts.row is not None and facts.row.state == PullRequest.State.REVIEW_REQUESTED):
        outcome = ("Waiting on colleagues", "Reviewer", "Review requested")
    elif facts.approvals_met is True and facts.ci.lower() in {"green", "success"}:
        outcome = (
            ("Ready to merge or merging", "Factory", "Merge through the ticket keystone")
            if facts.verified
            else ("Blocked", "Factory", "Forge facts not verified by a recent tick; waiting for the next read")
        )
    elif facts.entry is not None and facts.entry.next_task:
        group = "Factory working" if facts.ci.lower() in {"pending", "running"} else "Blocked"
        outcome = (group, "Factory", facts.entry.next_task)
    elif facts.ci == "unknown":
        outcome = ("Blocked", "Factory", "PR state unreadable; inspect live checks and approvals")
    else:
        outcome = ("Factory working", "Factory", "Inspect pull request")
    return outcome


def _ticket_state(
    urls: set[str], rows_by_url: dict[str, PullRequest]
) -> tuple[dict[str, Ticket], dict[str, CachedPr], dict[str, Ticket], set[int]]:
    tickets = list(
        Ticket.objects.filter(Q(issue_url__in=urls) | ~Q(extra={})).only(
            "id", "issue_url", "short_description", "extra"
        )
    )
    owners, cache = _cached_prs(tickets)
    by_issue_url = {ticket.issue_url: ticket for ticket in tickets if ticket.issue_url}
    ticket_ids = {
        ticket.pk
        for url in urls
        if (ticket := rows_by_url[url].ticket if url in rows_by_url else owners.get(url) or by_issue_url.get(url))
    }
    return owners, cache, by_issue_url, ticket_ids


def _active_tasks(ticket_ids: set[int]) -> tuple[dict[int, Task], dict[int, dict[str, int]], dict[int, TaskAttempt]]:
    tasks: dict[int, Task] = {}
    queued: dict[int, dict[str, int]] = {}
    for task in Task.objects.filter(ticket_id__in=ticket_ids, status__in=_ACTIVE_TASK_STATES).order_by(
        "ticket_id", "-pk"
    ):
        tasks.setdefault(task.ticket_id, task)
        if task.status == Task.Status.PENDING:
            queued.setdefault(task.ticket_id, {})[task.phase] = task.pk
    attempts: dict[int, TaskAttempt] = {}
    recent = TaskAttempt.objects.filter(task_id__in=[task.pk for task in tasks.values()]).order_by("task_id", "-pk")
    for attempt in recent:
        attempts.setdefault(attempt.task_id, attempt)
    return tasks, queued, attempts


def _visible_pr_urls(
    observed: dict[str, PrInventoryEntry], observed_overlays: set[str], rows_by_url: dict[str, PullRequest]
) -> set[str]:
    urls = set(observed)
    for row in rows_by_url.values():
        if row.overlay not in observed_overlays or row.url in observed:
            urls.add(row.url)
    return urls


def _review_posts(urls: set[str]) -> dict[str, ReviewRequestPost]:
    return {post.mr_url: post for post in ReviewRequestPost.objects.filter(mr_url__in=urls, done_at__isnull=True)}


def _collect_sources() -> PrBoardSources:
    now = timezone.now()
    observation = _inventory(now)
    rows_by_url = {
        row.url: row for row in PullRequest.objects.filter(state__in=_ACTIVE_PR_STATES).select_related("ticket")
    }
    urls = _visible_pr_urls(observation.entries, observation.overlays, rows_by_url)
    owners, cache, by_issue_url, ticket_ids = _ticket_state(urls, rows_by_url)
    if not observation.overlays:
        urls.update(
            url for url, raw in cache.items() if raw.get("state") in {"open", "opened", "review_requested", "approved"}
        )
        ticket_ids.update(ticket.pk for ticket in owners.values())
    tasks, queued, attempts = _active_tasks(ticket_ids)
    questions_by_url, questions_by_ticket = _owner_questions(urls, ticket_ids)
    return PrBoardSources(
        now,
        urls,
        observation.entries,
        observation.verified_urls,
        rows_by_url,
        owners,
        cache,
        by_issue_url,
        tasks,
        queued,
        _last_failure_by_ticket(list(ticket_ids)) if ticket_ids else {},
        _review_posts(urls),
        attempts,
        questions_by_url,
        questions_by_ticket,
        observation.warning,
    )


def _card_identity(
    entry: PrInventoryEntry | None, row: PullRequest | None, raw: CachedPr, ticket: Ticket | None
) -> tuple[str, str]:
    repo = entry.repo if entry is not None else row.repo if row is not None else str(raw.get("repo", ""))
    iid = entry.iid if entry is not None else row.iid if row is not None else raw.get("iid", "?")
    ref = f"{repo}!{iid}"
    title = entry.title if entry is not None else str(raw.get("title") or (ticket.short_description if ticket else ""))
    return title or ref, ref


def _approvals_met(raw: CachedPr) -> bool | None:
    approvals = raw.get("approvals")
    if not isinstance(approvals, dict):
        return None
    count, required = approvals.get("count"), approvals.get("required")
    if not isinstance(count, int) or not isinstance(required, int):
        return None
    return count >= required


def _review_due(post: ReviewRequestPost, repo: str) -> str:
    try:
        overlay = get_overlay(post.overlay or None)
        settings = get_effective_settings(post.overlay or None)
    except ImproperlyConfigured:
        return "unknown"
    thresholds = replace(DEFAULT_THRESHOLDS, nag_backoff_cap=timedelta(days=settings.review_nag_max_interval_days))
    owner = overlay.review.repo_owner_for_slug(repo)
    due = (post.last_nag_at or post.created_at) + thresholds.nag_interval_for_attempt(owner, post.nag_count)
    return due.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _last_result(attempt: TaskAttempt | None) -> str:
    if attempt is None:
        return "none"
    result = attempt.result if isinstance(attempt.result, dict) else {}
    summary = result.get("summary")
    if isinstance(summary, str) and summary:
        return summary[:160]
    return str(attempt.outcome or "in progress")


def _card(url: str, sources: PrBoardSources) -> PrBoardCard:
    entry = sources.observed.get(url)
    row = sources.rows_by_url.get(url)
    ticket = row.ticket if row is not None else sources.owners.get(url) or sources.tickets_by_issue_url.get(url)
    raw = sources.cache.get(url, {})
    ci = entry.ci if entry is not None and entry.ci != "unknown" else str(raw.get("pipeline_status") or "unknown")
    threads = _thread_text(raw)
    if threads == "unknown" and entry is not None:
        threads = entry.threads
    created = parse_time(entry.created_at if entry is not None else row.create_verified_at if row is not None else None)
    updated = entry.updated_at if entry is not None else raw.get("updated_at")
    post = sources.review_posts.get(url)
    approvals = _approval_text(raw)
    facts = PrActionFacts(
        entry=entry,
        row=row,
        task=sources.tasks.get(ticket.pk) if ticket is not None else None,
        failure=sources.failures.get(ticket.pk, ("", ""))[0] if ticket is not None else "",
        review_requested=post is not None or raw.get("review_requested") is True,
        question=(
            sources.questions_by_url.get(url, "") or sources.questions_by_ticket.get(ticket.pk if ticket else -1, "")
        ),
        ci=ci,
        approvals_met=_approvals_met(raw),
        approvals=approvals,
        threads=threads,
        verified=url in sources.verified_urls,
    )
    outcome = _next_action(facts)
    title, ref = _card_identity(entry, row, raw, ticket)
    return PrBoardCard(
        dom_id=sha256(url.encode()).hexdigest()[:16],
        url=url,
        link_url=_web_link(url),
        title=title,
        ref=ref,
        age=elapsed_label(created, sources.now),
        created_at=created,
        ci=ci,
        approvals=approvals,
        threads=threads,
        last_activity=elapsed_label(updated if isinstance(updated, (str, datetime)) else None, sources.now),
        next_actor=outcome[1],
        next_action=outcome[2],
        group=outcome[0],
        ticket_id=ticket.pk if ticket is not None else None,
        draft=entry.draft if entry is not None else bool(raw.get("draft")),
        review_link=_web_link(
            str(
                raw.get("review_permalink")
                or (
                    f"https://app.slack.com/archives/{post.slack_channel_id}/p{post.slack_thread_ts.replace('.', '')}"
                    if post and post.slack_channel_id and post.slack_thread_ts
                    else ""
                )
            )
        ),
        review_age=elapsed_label(post.created_at, sources.now) if post else "unknown",
        review_reasks=post.nag_count if post else 0,
        review_due=_review_due(post, entry.repo if entry else row.repo if row else str(raw.get("repo", "")))
        if post
        else "unknown",
        work_agent=(facts.task.claimed_by or "queued") if facts.task else "",
        work_since=elapsed_label(facts.task.claimed_at or facts.task.created_at, sources.now) if facts.task else "",
        last_result=_last_result(sources.attempts.get(facts.task.pk)) if facts.task else "",
        enqueue_buttons=board_enqueue_buttons(sources.queued.get(ticket.pk, {}), ticket=ticket) if ticket else (),
    )


def build_pr_board() -> PrBoard:
    sources = _collect_sources()
    grouped: dict[str, list[PrBoardCard]] = {name: [] for name in _GROUPS}
    for url in sources.urls:
        card = _card(url, sources)
        grouped[card.group].append(card)
    groups = tuple(
        PrBoardGroup(
            name=name,
            cards=tuple(
                sorted(grouped[name], key=lambda card: (card.created_at or datetime.max.replace(tzinfo=UTC), card.ref))
            ),
        )
        for name in _GROUPS
    )
    return PrBoard(groups=groups, total=len(sources.urls), inventory_warning=sources.warning)
