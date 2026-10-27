"""Bounded fix-first evidence for the intake landscape survey."""

import json
from datetime import datetime
from typing import TypedDict

from django.conf import settings
from django.db.models import Max

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.intake.factory_admission import payload_labels
from teatree.core.intake.nit_priority import is_nit_issue
from teatree.core.intake.ticket_kind_classification import classify_ticket_kind
from teatree.core.models import PullRequest, Task, Ticket
from teatree.core.models.shipping_hold import is_shipping_held

AUTHOR_STATE_PHASE: dict[str, str] = {
    Ticket.State.WORK_STARTED: "planning",
    Ticket.State.PLAN_RECORDED: "coding",
    Ticket.State.CODED: "testing",
    Ticket.State.TESTED: "reviewing",
    Ticket.State.SELF_REVIEWED: "shipping",
}
DEFAULT_STUCK_IDLE_HOURS = 6
FIX_FIRST_LIST_CAP = 5
FIX_FIRST_MAX_BYTES = 3072


class DefectEntry(TypedDict):
    issue: str
    title: str
    filed: str


class StuckEntry(TypedDict):
    ticket: int
    issue: str
    state: str
    idle_days: int


class FixFirst(TypedDict):
    defects: list[DefectEntry]
    more_defects: int
    stuck: list[StuckEntry]
    more_stuck: int
    unread: list[str]


def stuck_idle_hours() -> int:
    raw = getattr(settings, "STUCK_TICKET_IDLE_HOURS", DEFAULT_STUCK_IDLE_HOURS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_STUCK_IDLE_HOURS
    return max(1, value)


def ticket_last_activity(ticket: Ticket) -> datetime | None:
    last_attempt = ticket.tasks.aggregate(ts=Max("attempts__started_at"))["ts"]
    if last_attempt is not None:
        return last_attempt
    return ticket.transitions.aggregate(ts=Max("created_at"))["ts"]


def _defects(host: CodeHostBackend | None, repo_slugs: list[str]) -> tuple[list[DefectEntry], list[str]]:
    if host is None:
        return [], ["no code host configured"]
    if not repo_slugs:
        return [], ["no repository configured for defect listing"]
    defects: list[DefectEntry] = []
    unread: list[str] = []
    for slug in repo_slugs:
        try:
            issues = host.list_repo_open_issues(repo=slug)
        except Exception as exc:  # noqa: BLE001 — surface a failed external read in the survey
            unread.append(f"could not list defects in {slug}: {exc}"[:160])
            continue
        if not isinstance(issues, list):
            unread.append(f"invalid issue list from {slug}"[:160])
            continue
        for issue in issues:
            if not isinstance(issue, dict):
                unread.append(f"invalid issue entry from {slug}"[:160])
                continue
            title = issue.get("title")
            title = title if isinstance(title, str) else ""
            if (
                is_nit_issue(issue)
                or classify_ticket_kind(labels=payload_labels(issue), title=title) != Ticket.Kind.FIX
            ):
                continue
            url = issue.get("web_url") or issue.get("html_url") or issue.get("url")
            if not isinstance(url, str) or not url:
                unread.append(f"missing issue URL in {slug}"[:160])
                continue
            filed = issue.get("created_at") or issue.get("createdAt")
            defects.append(
                DefectEntry(
                    issue=url,
                    title=title[:80],
                    filed=filed if isinstance(filed, str) else "",
                )
            )
    defects.sort(key=lambda item: (not bool(item["filed"]), item["filed"], item["issue"]))
    return defects, unread[:3]


def _stuck(now: datetime) -> list[StuckEntry]:
    threshold = stuck_idle_hours() * 3600
    tickets = (
        Ticket.objects.filter(role=Ticket.Role.AUTHOR, state__in=tuple(AUTHOR_STATE_PHASE))
        .exclude(tasks__status__in=Task.Status.active())
        .exclude(pull_requests__in=PullRequest.objects.live())
        .distinct()
    )
    aged: list[tuple[datetime, StuckEntry]] = []
    for ticket in tickets:
        phase = AUTHOR_STATE_PHASE[ticket.state]
        if (
            is_shipping_held(ticket)
            or ticket.newest_task_was_cancelled()
            or ticket.newest_phase_task_was_cancelled(phase)
        ):
            continue
        last = ticket_last_activity(ticket)
        if last is None or (now - last).total_seconds() < threshold:
            continue
        aged.append(
            (
                last,
                StuckEntry(
                    ticket=ticket.pk,
                    issue=ticket.issue_url,
                    state=ticket.state,
                    idle_days=int((now - last).total_seconds() // 86400),
                ),
            )
        )
    aged.sort(key=lambda pair: (pair[0], pair[1]["ticket"]))
    return [item for _, item in aged]


def fix_first_listing(host: CodeHostBackend | None, repo_slugs: list[str], *, now: datetime) -> FixFirst:
    defects, unread = _defects(host, repo_slugs)
    stuck = _stuck(now)
    result = FixFirst(
        defects=defects[:FIX_FIRST_LIST_CAP],
        more_defects=max(0, len(defects) - FIX_FIRST_LIST_CAP),
        stuck=stuck[:FIX_FIRST_LIST_CAP],
        more_stuck=max(0, len(stuck) - FIX_FIRST_LIST_CAP),
        unread=unread,
    )
    while len(json.dumps(result).encode()) > FIX_FIRST_MAX_BYTES:
        if result["stuck"] and (not result["defects"] or len(result["stuck"]) >= len(result["defects"])):
            result["stuck"].pop()
            result["more_stuck"] += 1
        elif result["defects"]:
            result["defects"].pop()
            result["more_defects"] += 1
        else:
            break
    return result
