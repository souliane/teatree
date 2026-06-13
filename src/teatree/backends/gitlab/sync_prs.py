"""PR sync functions extracted from ``gitlab_sync.py``.

Handles PR entry construction, ticket upsert from PRs, discussion
classification, E2E evidence detection, and state inference from PR data.
"""

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, SupportsInt, cast

from teatree.backends.gitlab.sync_approvals import detect_approval_dismissal
from teatree.core.gates.dod_gate import workflow_capped_state
from teatree.core.models import Ticket
from teatree.types import DiscussionSummary, PREntry, PREntryDict, RawAPIDict, SyncResult

if TYPE_CHECKING:
    from teatree.backends.gitlab.api import GitLabAPI, ProjectInfo
    from teatree.core.models.types import TicketExtra, TicketSiblingFields

logger = logging.getLogger(__name__)

_REPO_PATH_RE = re.compile(r"https?://[^/]+/(.+?)/-/merge_requests/")
_ISSUE_URL_RE = re.compile(r"(https://[^\s)]+/-/(?:issues|work_items)/\d+)")
_E2E_TEST_PLAN_RE = re.compile(
    r"e2e|test.?evidence|playwright|screenshot|side.by.side|figma",
    re.IGNORECASE,
)
_SKILL_WRITTEN_FIELDS = ("review_channel", "review_permalink", "e2e_test_plan_url", "notion_status", "notion_url")
_STATE_ORDER = [s.value for s in Ticket.State]


@dataclass(frozen=True, slots=True)
class _PRContext:
    """A single GitLab MR being processed as a teatree PR.

    The ``raw`` field holds the upstream GitLab API response (which uses
    GitLab's MR vocabulary); the surrounding code maps it onto the teatree
    canonical ``PREntry`` model.
    """

    raw: RawAPIDict
    repo_short: str
    client: "GitLabAPI"
    project: "ProjectInfo | None"


def extract_repo_path(raw: RawAPIDict) -> str:
    web_url = str(raw.get("web_url", ""))
    match = _REPO_PATH_RE.search(web_url)
    return match.group(1) if match else ""


def build_pr_entry(ctx: "_PRContext", *, username: str) -> PREntry:
    """Build a fully enriched PREntry from a raw GitLab MR dict."""
    raw = ctx.raw
    web_url = str(raw.get("web_url", ""))
    is_draft = bool(raw.get("draft"))
    pr_iid = int(cast("SupportsInt", raw.get("iid", 0)))

    pr_entry = PREntry(
        url=web_url,
        title=str(raw.get("title", "")),
        branch=str(raw.get("source_branch", "")),
        draft=is_draft,
        repo=ctx.repo_short,
        iid=pr_iid,
        updated_at=str(raw.get("updated_at", "")),
        state=str(raw.get("state", "opened")),
    )

    if not is_draft and ctx.project and pr_iid:
        pipeline = ctx.client.get_mr_pipeline(ctx.project.project_id, pr_iid)
        pr_entry.pipeline_status = pipeline["status"]
        pr_entry.pipeline_url = pipeline["url"]
        pr_entry.approvals = ctx.client.get_mr_approvals(ctx.project.project_id, pr_iid)

        discussions = ctx.client.get_mr_discussions(ctx.project.project_id, pr_iid)
        pr_entry.discussions = classify_discussions(discussions, username)
        e2e_url = detect_e2e_test_plan(discussions, web_url)
        if e2e_url:
            pr_entry.e2e_test_plan_url = e2e_url

        current_count = (
            int(pr_entry.approvals.get("count", 0)) if isinstance(pr_entry.approvals, dict) else 0  # ty: ignore[invalid-argument-type]
        )
        dismissal = detect_approval_dismissal(discussions, current_approval_count=current_count)
        if dismissal is not None:
            pr_entry.approvals_dismissed_at = dismissal.at
            pr_entry.dismissed_approvers = dismissal.approvers

        draft_count = ctx.client.get_draft_notes_count(ctx.project.project_id, pr_iid)
        pr_entry.draft_comments_pending = draft_count > 0
        pr_entry.draft_comments_count = draft_count if draft_count > 0 else None

    reviewers = raw.get("reviewers", [])
    if isinstance(reviewers, list):
        pr_entry.review_requested = bool(reviewers)
        pr_entry.reviewer_names = [str(r.get("username", "")) for r in reviewers if isinstance(r, dict)]  # ty: ignore[no-matching-overload]

    return pr_entry


def upsert_ticket_from_pr(
    ctx: "_PRContext",
    result: SyncResult,
    *,
    username: str = "",
    overlay_name: str = "",
) -> None:
    pr_entry = build_pr_entry(ctx, username=username)
    web_url = pr_entry.url
    lookup_url = extract_issue_url(ctx.raw) or web_url
    pr_entry_dict = pr_entry.to_dict()
    inferred_state = infer_state_from_prs({web_url: pr_entry_dict})

    tickets = list(Ticket.objects.filter(issue_url=lookup_url).order_by("pk"))
    if not tickets:
        new_ticket = Ticket(
            issue_url=lookup_url,
            repos=[ctx.repo_short],
            extra={"prs": {web_url: pr_entry_dict}},
            overlay=overlay_name,
        )
        new_ticket.state = workflow_capped_state(new_ticket, inferred_state)
        new_ticket.save()
        result.tickets_created += 1
    else:
        ticket = tickets[0]
        for dup in tickets[1:]:
            merge_ticket_extras(ticket, dup)
            dup.delete()
        if overlay_name and not ticket.overlay:
            ticket.overlay = overlay_name
            ticket.save(update_fields=["overlay"])
        update_ticket(ticket, pr_entry_dict, web_url, ctx.repo_short, inferred_state)
        result.tickets_updated += 1


def classify_discussions(
    discussions: list[RawAPIDict],
    author_username: str,
) -> list[DiscussionSummary]:
    result: list[DiscussionSummary] = []
    for disc in discussions:
        if not isinstance(disc, dict):
            continue
        if disc.get("individual_note"):
            continue
        notes = disc.get("notes", [])
        if not isinstance(notes, list) or not notes:
            continue

        first_body = str(notes[0].get("body", "")) if isinstance(notes[0], dict) else ""  # ty: ignore[no-matching-overload]
        resolvable_notes = [n for n in notes if isinstance(n, dict) and n.get("resolvable")]  # ty: ignore[invalid-argument-type]
        all_resolved = bool(resolvable_notes) and all(n.get("resolved") for n in resolvable_notes)  # ty: ignore[invalid-argument-type]

        if all_resolved:
            status = "addressed"
        else:
            last_note = notes[-1]
            author_info = last_note.get("author", {}) if isinstance(last_note, dict) else {}  # ty: ignore[no-matching-overload]
            last_author = str(author_info.get("username", "")) if isinstance(author_info, dict) else ""
            status = "waiting_reviewer" if last_author == author_username else "needs_reply"

        result.append(DiscussionSummary(status=status, detail=first_body[:120]))
    return result


def detect_e2e_test_plan(discussions: list[RawAPIDict], pr_url: str) -> str:
    for disc in discussions:
        if not isinstance(disc, dict):
            continue
        notes = disc.get("notes", [])
        if not isinstance(notes, list):
            continue
        for note in notes:
            if not isinstance(note, dict):
                continue
            body = str(note.get("body", ""))  # ty: ignore[no-matching-overload]
            has_image = "![" in body or "/uploads/" in body
            has_keyword = bool(_E2E_TEST_PLAN_RE.search(body))
            if has_keyword and has_image:
                note_id = note.get("id")  # ty: ignore[invalid-argument-type]
                return f"{pr_url}#note_{note_id}" if note_id else pr_url
    return ""


def merge_ticket_extras(target: Ticket, source: Ticket) -> None:
    src_extra = source.extra if isinstance(source.extra, dict) else {}
    tgt_extra = target.extra if isinstance(target.extra, dict) else {}

    src_prs = src_extra.get("prs", {})
    tgt_prs = tgt_extra.get("prs", {})
    set_prs = isinstance(src_prs, dict) and isinstance(tgt_prs, dict)
    if set_prs:
        for url, entry in src_prs.items():
            if url not in tgt_prs:
                tgt_prs[url] = entry

    src_repos = source.repos if isinstance(source.repos, list) else []
    tgt_repos = target.repos if isinstance(target.repos, list) else []
    for repo in src_repos:
        if repo not in tgt_repos:
            tgt_repos.append(repo)

    # #800 N3: canonical locked RMW; extra (prs) + repos one atomic
    # write via also_set (no split, no unlocked clobber).
    set_keys = cast("TicketExtra", {"prs": tgt_prs}) if set_prs else None
    target.merge_extra(set_keys=set_keys, also_set={"repos": tgt_repos})


def update_ticket(
    ticket: Ticket,
    pr_entry: PREntryDict,
    pr_url: str,
    repo_short: str,
    inferred_state: str = "",
) -> None:
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    prs = extra.get("prs", {})
    if not isinstance(prs, dict):
        prs = {}

    prev = prs.get(pr_url)
    if isinstance(prev, dict):
        for key in _SKILL_WRITTEN_FIELDS:
            if key in prev and key not in pr_entry:
                pr_entry[key] = prev[key]

    prs[pr_url] = pr_entry

    repos = ticket.repos if isinstance(ticket.repos, list) else []
    if repo_short not in repos:
        repos = [*repos, repo_short]
    # The DoD gate's UI-visibility check reads ``ticket.repos``; reflect the
    # synced repo set in-memory first so a newly-scoped frontend repo cannot
    # let a SHIPPED write slip past the gate on the stale (smaller) set.
    ticket.repos = repos

    # #800 N3: canonical locked RMW; narrow set_keys to the only top-level
    # key this fn mutates (prs) so merge_extra's locked re-read does not
    # clobber a concurrent writer's sibling key from the stale snapshot
    # (#1505). repos (+ optional state) ride along via also_set in the
    # same atomic write (no split).
    also_set: TicketSiblingFields = {"repos": repos}
    capped_state = workflow_capped_state(ticket, inferred_state) if inferred_state else inferred_state
    if capped_state and _STATE_ORDER.index(capped_state) > _STATE_ORDER.index(ticket.state):
        also_set["state"] = capped_state

    set_keys = cast("TicketExtra", {"prs": prs})
    ticket.merge_extra(set_keys=set_keys, also_set=also_set)


def infer_state_from_prs(prs_data: dict[str, PREntryDict]) -> str:
    best = Ticket.State.NOT_STARTED
    for pr in prs_data.values():
        if not isinstance(pr, dict):
            continue
        is_draft = pr.get("draft", True)
        if is_draft:
            candidate = Ticket.State.STARTED
        else:
            approvals = pr.get("approvals")
            has_approvals = isinstance(approvals, dict) and int(approvals.get("count", 0)) > 0  # ty: ignore[no-matching-overload]
            review_requested = bool(pr.get("review_requested"))
            candidate = Ticket.State.IN_REVIEW if (has_approvals or review_requested) else Ticket.State.SHIPPED
        if _STATE_ORDER.index(candidate) > _STATE_ORDER.index(best):
            best = candidate
    return best


def extract_issue_url(raw: RawAPIDict) -> str:
    for text in [
        str(raw.get("description", "") or "").split("\n", maxsplit=1)[0],
        str(raw.get("title", "")),
    ]:
        match = _ISSUE_URL_RE.search(text)
        if match:
            return match.group(1)
    return ""
