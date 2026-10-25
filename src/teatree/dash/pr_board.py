"""Open pull requests grouped by the next actor and action."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from teatree.config import get_effective_settings
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.review_request_post import ReviewRequestPost
from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.overlay_loader import get_overlay
from teatree.core.review.mr_triage import DEFAULT_THRESHOLDS
from teatree.core.review.pr_inventory import PrInventoryEntry, elapsed_label, merged_entries, parse_time, read_inventory
from teatree.core.review.pr_next_action import GROUPS, UNKNOWN, PrActionFacts, decide
from teatree.dash.selectors import last_failure_by_ticket
from teatree.dash.task_actions import EnqueueButton, board_enqueue_buttons, oldest_pending_phase_tasks

_ACTIVE_PR_STATES = (PullRequest.State.OPEN, PullRequest.State.REVIEW_REQUESTED, PullRequest.State.APPROVED)
_ACTIVE_TASK_STATES = (Task.Status.PENDING, Task.Status.CLAIMED)
_STALE_INVENTORY_AFTER = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class PrBoardCard:
    dom_id: str
    url: str
    link_url: str
    title: str
    ref: str
    author: str
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
    watched: dict[str, tuple[datetime, frozenset[str]]]
    verified_urls: set[str]
    warning: str


def _inventory(now: datetime) -> PrObservation:
    try:
        snapshots = read_inventory()
    except (OSError, ValueError) as exc:
        return PrObservation({}, {}, set(), f"PR inventory unavailable: {exc}")
    if not snapshots:
        return PrObservation({}, {}, set(), "PR inventory unavailable; showing recorded pull requests.")
    fresh = [
        snapshot for snapshot in snapshots if snapshot.verified and now - snapshot.observed_at <= _STALE_INVENTORY_AFTER
    ]
    stale = [snapshot for snapshot in snapshots if snapshot.verified and snapshot not in fresh]
    unknown = [snapshot for snapshot in snapshots if not snapshot.verified]
    verified = merged_entries(fresh)
    watched = {
        snapshot.overlay: (
            snapshot.observed_at,
            frozenset(snapshot.watched_repos or (entry.repo for entry in snapshot.entries)),
        )
        for snapshot in fresh
        if not snapshot.overlay.startswith("own:")
    }
    warnings = []
    if stale:
        warnings.append(f"PR inventory is stale for {', '.join(sorted(item.overlay for item in stale))}.")
    if unknown:
        warnings.append(f"PR inventory freshness is unknown for {', '.join(sorted(item.overlay for item in unknown))}.")
    return PrObservation({**merged_entries((*stale, *unknown)), **verified}, watched, set(verified), " ".join(warnings))


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


@dataclass(slots=True)
class PrBoardSources:
    now: datetime
    urls: set[str]
    observed: dict[str, PrInventoryEntry]
    verified_urls: set[str]
    rows_by_url: dict[str, PullRequest]
    tickets_by_issue_url: dict[str, Ticket]
    tasks: dict[int, Task]
    queued: dict[int, dict[str, int]]
    failures: dict[int, tuple[str, str]]
    review_posts: dict[str, ReviewRequestPost]
    attempts: dict[int, TaskAttempt]
    questions_by_url: dict[str, str]
    questions_by_ticket: dict[int, str]
    warning: str


def _ticket_state(urls: set[str], rows_by_url: dict[str, PullRequest]) -> tuple[dict[str, Ticket], set[int]]:
    tickets = list(
        Ticket.objects.filter(issue_url__in=urls).only("id", "issue_url", "short_description", "state", "role")
    )
    by_issue_url = {ticket.issue_url: ticket for ticket in tickets if ticket.issue_url}
    ticket_ids = {
        ticket.pk
        for url in urls
        if (ticket := rows_by_url[url].ticket if url in rows_by_url else by_issue_url.get(url))
    }
    return by_issue_url, ticket_ids


def _active_tasks(ticket_ids: set[int]) -> tuple[dict[int, Task], dict[int, dict[str, int]], dict[int, TaskAttempt]]:
    active = list(
        Task.objects.filter(ticket_id__in=ticket_ids, status__in=_ACTIVE_TASK_STATES).order_by("ticket_id", "-pk")
    )
    tasks: dict[int, Task] = {}
    for task in active:
        tasks.setdefault(task.ticket_id, task)
    attempts: dict[int, TaskAttempt] = {}
    recent = TaskAttempt.objects.filter(task_id__in=[task.pk for task in tasks.values()]).order_by("task_id", "-pk")
    for attempt in recent:
        attempts.setdefault(attempt.task_id, attempt)
    return tasks, oldest_pending_phase_tasks(active), attempts


def _visible_pr_urls(
    observed: dict[str, PrInventoryEntry],
    watched: dict[str, tuple[datetime, frozenset[str]]],
    rows_by_url: dict[str, PullRequest],
) -> set[str]:
    """Observed PRs, plus every recorded row no fresh verified sweep that watched its repository can contradict."""
    urls = set(observed)
    for row in rows_by_url.values():
        observation = watched.get(row.overlay)
        seen_at = observation[0] if observation and row.repo in observation[1] else None
        newer_than_snapshot = (
            seen_at is not None and row.create_verified_at is not None and row.create_verified_at > seen_at
        )
        if seen_at is None or newer_than_snapshot or row.url in observed:
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
    urls = _visible_pr_urls(observation.entries, observation.watched, rows_by_url)
    by_issue_url, ticket_ids = _ticket_state(urls, rows_by_url)
    tasks, queued, attempts = _active_tasks(ticket_ids)
    questions_by_url, questions_by_ticket = _owner_questions(urls, ticket_ids)
    return PrBoardSources(
        now,
        urls,
        observation.entries,
        observation.verified_urls,
        rows_by_url,
        by_issue_url,
        tasks,
        queued,
        last_failure_by_ticket(list(ticket_ids)) if ticket_ids else {},
        _review_posts(urls),
        attempts,
        questions_by_url,
        questions_by_ticket,
        observation.warning,
    )


def _card_identity(entry: PrInventoryEntry | None, row: PullRequest | None, ticket: Ticket | None) -> tuple[str, str]:
    repo = entry.repo if entry is not None else row.repo if row is not None else ""
    iid = entry.iid if entry is not None else row.iid if row is not None else "?"
    ref = f"{repo}!{iid}"
    title = entry.title if entry is not None else ticket.short_description if ticket else ""
    return title or ref, ref


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


@dataclass(frozen=True, slots=True)
class _Subject:
    url: str
    entry: PrInventoryEntry | None
    row: PullRequest | None
    ticket: Ticket | None
    post: ReviewRequestPost | None
    task: Task | None

    @property
    def repo(self) -> str:
        return self.entry.repo if self.entry else self.row.repo if self.row else ""


def _subject(url: str, sources: PrBoardSources) -> _Subject:
    entry = sources.observed.get(url)
    row = sources.rows_by_url.get(url)
    ticket = row.ticket if row is not None else sources.tickets_by_issue_url.get(url)
    return _Subject(
        url=url,
        entry=entry,
        row=row,
        ticket=ticket,
        post=sources.review_posts.get(url),
        task=sources.tasks.get(ticket.pk) if ticket is not None else None,
    )


def _facts(subject: _Subject, sources: PrBoardSources) -> PrActionFacts:
    entry, ticket, task = subject.entry, subject.ticket, subject.task
    if entry is not None:
        ci, approvals, threads, threads_blocking = (
            entry.ci,
            entry.approvals,
            entry.threads,
            entry.decision == "my_pr.findings_to_fix",
        )
    else:
        ci, approvals, threads, threads_blocking = UNKNOWN, UNKNOWN, UNKNOWN, False
    return PrActionFacts(
        entry=entry,
        working=f"{task.phase} ({task.status})" if task is not None else "",
        failure=sources.failures.get(ticket.pk, ("", ""))[0] if ticket is not None else "",
        review_requested=subject.post is not None
        or (subject.row is not None and subject.row.state == PullRequest.State.REVIEW_REQUESTED),
        question=(
            sources.questions_by_url.get(subject.url, "")
            or sources.questions_by_ticket.get(ticket.pk if ticket else -1, "")
        ),
        ci=ci,
        approvals=approvals,
        threads=threads,
        threads_blocking=threads_blocking,
        draft=entry.draft if entry is not None else False,
        verified=subject.url in sources.verified_urls,
        owned=entry.owned if entry is not None else None,
    )


def _review_link(subject: _Subject) -> str:
    post = subject.post
    slack = (
        f"https://app.slack.com/archives/{post.slack_channel_id}/p{post.slack_thread_ts.replace('.', '')}"
        if post and post.slack_channel_id and post.slack_thread_ts
        else ""
    )
    return _web_link(slack)


def _card(url: str, sources: PrBoardSources) -> PrBoardCard:
    subject = _subject(url, sources)
    entry, row, ticket, post, task = subject.entry, subject.row, subject.ticket, subject.post, subject.task
    facts = _facts(subject, sources)
    outcome = decide(facts)
    created = parse_time(entry.created_at if entry is not None else row.create_verified_at if row is not None else None)
    updated = entry.updated_at if entry is not None else None
    title, ref = _card_identity(entry, row, ticket)
    return PrBoardCard(
        dom_id=sha256(url.encode()).hexdigest()[:16],
        url=url,
        link_url=_web_link(url),
        title=title,
        ref=ref,
        author=entry.author if entry is not None else "",
        age=elapsed_label(created, sources.now),
        created_at=created,
        ci=facts.ci,
        approvals=(entry.approvals_display if entry is not None else "") or facts.approvals,
        threads=facts.threads,
        last_activity=elapsed_label(updated if isinstance(updated, (str, datetime)) else None, sources.now),
        next_actor=outcome.actor,
        next_action=outcome.text,
        group=outcome.group,
        ticket_id=ticket.pk if ticket is not None else None,
        draft=facts.draft,
        review_link=_review_link(subject),
        review_age=elapsed_label(post.created_at, sources.now) if post else UNKNOWN,
        review_reasks=post.nag_count if post else 0,
        review_due=_review_due(post, subject.repo) if post else UNKNOWN,
        work_agent=(task.claimed_by or "queued") if task else "",
        work_since=elapsed_label(task.claimed_at or task.created_at, sources.now) if task else "",
        last_result=_last_result(sources.attempts.get(task.pk)) if task else "",
        enqueue_buttons=board_enqueue_buttons(sources.queued.get(ticket.pk, {}), ticket=ticket) if ticket else (),
    )


def build_pr_board() -> PrBoard:
    sources = _collect_sources()
    grouped: dict[str, list[PrBoardCard]] = {name: [] for name in GROUPS}
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
        for name in GROUPS
    )
    return PrBoard(groups=groups, total=len(sources.urls), inventory_warning=sources.warning)
