"""GitHub sync backend — project board issues and reviewer PR discovery."""

import json
import logging
import os
import shutil
import subprocess  # noqa: S404
from typing import TYPE_CHECKING, override

from django.core.cache import cache

from teatree.core.cleanup import cleanup_worktree
from teatree.core.sync import PENDING_REVIEWS_CACHE_KEY, RawAPIDict, SyncBackend, SyncResult

if TYPE_CHECKING:
    from teatree.core.models import Ticket

logger = logging.getLogger(__name__)


class GitHubSyncBackend(SyncBackend):
    @override
    def is_configured(self, overlay: object) -> bool:
        from teatree.core.overlay import OverlayBase  # noqa: PLC0415

        return isinstance(overlay, OverlayBase) and bool(overlay.config.get_github_token())

    @override
    def sync(self, overlay: object) -> SyncResult:
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
                prior_state = ticket.state
                existing_extra = ticket.extra if isinstance(ticket.extra, dict) else {}
                existing_extra.update(extra)
                ticket.extra = existing_extra
                ticket.state = state
                ticket.save(update_fields=["extra", "state"])
                result.tickets_updated += 1
                if state == Ticket.State.DELIVERED and prior_state != Ticket.State.DELIVERED:
                    self._cleanup_ticket_worktrees(ticket, result)

        self._sync_reviewer_prs(token, result)

        return result

    @classmethod
    def _cleanup_ticket_worktrees(cls, ticket: "Ticket", result: SyncResult) -> None:
        """Free worktrees when the project board moves a ticket to Done.

        Squash-merge-aware cleanup via :func:`cleanup_worktree`. Worktrees whose
        branches carry genuinely-unpushed work are kept (the RuntimeError path);
        the user resolves those by running ``t3 teatree workspace clean-all``.
        """
        from teatree.core.models import Worktree  # noqa: PLC0415

        for worktree in Worktree.objects.filter(ticket=ticket):
            try:
                cleanup_worktree(worktree)
                result.worktrees_cleaned += 1
            except RuntimeError as exc:
                logger.info("Keeping worktree %s (unpushed work): %s", worktree.repo_path, exc)
            except Exception:
                logger.exception("Failed to clean worktree %s", worktree.repo_path)
                result.errors.append(f"Worktree cleanup failed: {worktree.repo_path}")

    @classmethod
    def _fetch_reviewer_prs(cls, token: str) -> list[dict[str, str]]:
        gh_bin = shutil.which("gh")
        if not gh_bin:
            return []

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
        if out.returncode != 0:
            return []

        prs: list[RawAPIDict] = json.loads(out.stdout)
        return [
            {
                "url": str(pr.get("url", "")),
                "title": str(pr.get("title", "")),
                "repo": repo.get("name", "") if isinstance(repo := pr.get("repository", {}), dict) else "",
                "iid": str(pr.get("number", "")),
                "author": author.get("login", "") if isinstance(author := pr.get("author", {}), dict) else "",
                "draft": str(pr.get("isDraft", False)),
                "updated_at": str(pr.get("updatedAt", "")),
            }
            for pr in prs
        ]

    @classmethod
    def _sync_reviewer_prs(cls, token: str, result: SyncResult) -> None:
        try:
            reviews = cls._fetch_reviewer_prs(token)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"GitHub reviewer PR fetch failed: {exc}")
            return

        existing: list[dict[str, str]] = cache.get(PENDING_REVIEWS_CACHE_KEY) or []
        existing_urls = {r["url"] for r in existing}
        merged = [*existing, *(r for r in reviews if r["url"] not in existing_urls)]
        cache.set(PENDING_REVIEWS_CACHE_KEY, merged, timeout=None)
        result.reviews_synced += len(reviews)
