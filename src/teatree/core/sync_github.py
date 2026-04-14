"""GitHub sync backend — project board issues and reviewer PR discovery."""

import json
import logging
import os
import shutil
import subprocess  # noqa: S404

from django.core.cache import cache

from teatree.core.sync import PENDING_REVIEWS_CACHE_KEY, RawAPIDict, SyncResult

logger = logging.getLogger(__name__)


def _sync_github(overlay: object) -> SyncResult:
    """Sync issues from a GitHub Projects v2 board into Tickets."""
    from teatree.backends.github import fetch_project_items  # noqa: PLC0415
    from teatree.core.models import Ticket  # noqa: PLC0415
    from teatree.core.overlay import OverlayBase  # noqa: PLC0415

    if not isinstance(overlay, OverlayBase):
        return SyncResult(errors=["Invalid overlay"])

    token = overlay.config.get_github_token()
    owner = overlay.config.github_owner
    project_number = overlay.config.github_project_number

    if not owner or not project_number:
        return SyncResult(errors=["GitHub owner or project number not configured"])

    result = SyncResult()

    try:
        items = fetch_project_items(owner, project_number, token=token)
    except Exception as exc:  # noqa: BLE001
        return SyncResult(errors=[f"GitHub project fetch failed: {exc}"])

    status_map = {
        "Todo": Ticket.State.NOT_STARTED,
        "In Progress": Ticket.State.STARTED,
        "Done": Ticket.State.DELIVERED,
    }

    for item in items:
        result.mrs_found += 1
        state = status_map.get(item.status, Ticket.State.NOT_STARTED)
        extra: RawAPIDict = {
            "issue_title": item.title,
            "board_position": item.position,
            "board_status": item.status,
            "labels": item.labels,
            "updated_at": item.updated_at,
        }

        tickets = list(Ticket.objects.filter(issue_url=item.url).order_by("pk"))
        if not tickets:
            Ticket.objects.create(
                issue_url=item.url,
                repos=[owner.split("/")[-1]],
                state=state,
                extra=extra,
            )
            result.tickets_created += 1
        else:
            ticket = tickets[0]
            # Preserve existing extra fields not set by sync
            existing_extra = ticket.extra if isinstance(ticket.extra, dict) else {}
            existing_extra.update(extra)
            ticket.extra = existing_extra
            ticket.state = state
            ticket.save(update_fields=["extra", "state"])
            result.tickets_updated += 1

    _sync_github_reviewer_prs(token, result)

    return result


def _sync_github_reviewer_prs(token: str, result: SyncResult) -> None:
    """Fetch GitHub PRs where user is requested reviewer and cache them."""
    gh_bin = shutil.which("gh")
    if not gh_bin:
        return

    try:
        out = subprocess.run(  # noqa: S603
            [
                gh_bin,
                "search",
                "prs",
                "--review-requested=@me",
                "--state=open",
                "--json",
                "url,title,repository,number,author,isDraft,updatedAt",
                "--limit",
                "50",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={**os.environ, "GH_TOKEN": token},
        )
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"GitHub reviewer PR fetch failed: {exc}")
        return

    if out.returncode != 0:
        return

    try:
        prs = json.loads(out.stdout)
    except json.JSONDecodeError:
        return

    reviews: list[dict[str, str]] = []
    for pr in prs:
        repo = pr.get("repository", {})
        repo_name = repo.get("name", "") if isinstance(repo, dict) else ""
        author = pr.get("author", {})
        author_login = author.get("login", "") if isinstance(author, dict) else ""
        reviews.append(
            {
                "url": str(pr.get("url", "")),
                "title": str(pr.get("title", "")),
                "repo": repo_name,
                "iid": str(pr.get("number", "")),
                "author": author_login,
                "draft": str(pr.get("isDraft", False)),
                "updated_at": str(pr.get("updatedAt", "")),
            }
        )

    # Merge with any GitLab reviews already cached
    existing = cache.get(PENDING_REVIEWS_CACHE_KEY) or []
    existing_urls = {r["url"] for r in existing}
    for r in reviews:
        if r["url"] not in existing_urls:
            existing.append(r)
    cache.set(PENDING_REVIEWS_CACHE_KEY, existing, timeout=None)
    result.reviews_synced += len(reviews)
