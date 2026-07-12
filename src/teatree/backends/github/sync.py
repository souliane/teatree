"""GitHub sync backend — project board issues and reviewer PR discovery."""

import json
import logging
import os
import shutil
from typing import TYPE_CHECKING, cast, override

from django.core.cache import cache

from teatree.core.cleanup.cleanup import WorktreeBusyError, cleanup_worktree
from teatree.types import PENDING_REVIEWS_CACHE_KEY, RawAPIDict, SyncBackend, SyncResult
from teatree.utils.run import run_allowed_to_fail

if TYPE_CHECKING:
    from teatree.core.models import Ticket
    from teatree.core.models.types import TicketExtra

logger = logging.getLogger(__name__)


class GitHubSyncBackend(SyncBackend):
    @staticmethod
    def _overlay_name(overlay: object) -> str:
        from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415 — deferred: backends ↔ core cycle

        for name, ov in get_all_overlays().items():
            if ov is overlay:
                return name
        return ""

    @override
    def is_configured(self, overlay: object) -> bool:
        from teatree.core.overlay import OverlayBase  # noqa: PLC0415 — deferred: avoids a backends ↔ core cycle

        return isinstance(overlay, OverlayBase) and bool(overlay.config.get_github_token())

    @override
    def sync(self, overlay: object) -> SyncResult:
        from teatree.backends.github import fetch_project_items, issue_repo_short  # noqa: PLC0415 — import cycle
        from teatree.core.intake.ticket_kind_classification import classify_ticket_kind  # noqa: PLC0415 — lazy: cycle
        from teatree.core.models import Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry
        from teatree.core.overlay import OverlayBase  # noqa: PLC0415 — deferred: avoids a backends ↔ core cycle

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
        except Exception as exc:  # noqa: BLE001 — a project-fetch failure is recorded as a sync error, never crashes the sync
            return SyncResult(errors=[f"GitHub project fetch failed: {exc}"])

        status_map = {
            "Todo": Ticket.State.NOT_STARTED,
            "In Progress": Ticket.State.STARTED,
            "Done": Ticket.State.DELIVERED,
        }

        for item in items:
            result.prs_found += 1
            state = status_map.get(item.status, Ticket.State.NOT_STARTED)
            # The board item's URL is the authoritative repo source; the
            # project owner is not (a board spans repos), and scoping by owner
            # mis-classifies UI-visibility for the DoD gate (#1426).
            repo_short = issue_repo_short(item.url) or owner.split("/")[-1]
            extra: RawAPIDict = {
                "issue_title": item.title,
                "board_position": item.position,
                "board_status": item.status,
                "labels": item.labels,
                "updated_at": item.updated_at,
            }

            tickets = list(Ticket.objects.filter(issue_url=item.url).order_by("pk"))
            if not tickets:
                created = Ticket.objects.create(
                    issue_url=item.url,
                    repos=[repo_short],
                    state=state,
                    extra=extra,
                    overlay=self._overlay_name(overlay),
                    kind=classify_ticket_kind(labels=item.labels, title=item.title),
                )
                result.tickets_created += 1
                if state == Ticket.State.DELIVERED:
                    self._record_delivered_dod_violation(created)
            else:
                ticket = tickets[0]
                prior_state = ticket.state
                # Repair the repo scope from the authoritative URL before the
                # DoD check so the gate sees the real repo set (#1426).
                repos = ticket.repos if isinstance(ticket.repos, list) else []
                if repo_short and repo_short not in repos:
                    repos = [*repos, repo_short]
                ticket.repos = repos
                # #800 N3: canonical locked RMW; extra + repos + state stay one
                # atomic write via also_set (no split).
                ticket.merge_extra(
                    set_keys=cast("TicketExtra", dict(extra)),
                    also_set={"state": state, "repos": repos},
                )
                result.tickets_updated += 1
                if state == Ticket.State.DELIVERED:
                    # Audit runs on EVERY Done sync (idempotent — dedups on the
                    # existing marker), so a ticket the old owner-scoping bug
                    # already left at DELIVERED still gets the marker once its
                    # repo scope is repaired. Worktree cleanup, by contrast,
                    # stays gated on the transition so it never re-runs (#1426).
                    self._record_delivered_dod_violation(ticket)
                    if prior_state != Ticket.State.DELIVERED:
                        self._cleanup_ticket_worktrees(ticket, result)

        self._sync_reviewer_prs(token, result)

        return result

    @staticmethod
    def _record_delivered_dod_violation(ticket: "Ticket") -> None:
        """Audit a DELIVERED board move that skipped the DoD local-E2E gate (#1426).

        The project board reporting "Done" is an external terminal fact the
        sync follows, so the ticket is not demoted; but when the DoD was unmet
        the gap is recorded as a durable marker + loud log rather than being
        silently bypassed (mirrors the merged-PR terminal handling).
        """
        from teatree.core.gates.dod_gate import record_terminal_dod_violation  # noqa: PLC0415 — import cycle
        from teatree.core.models import Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

        record_terminal_dod_violation(ticket, Ticket.State.DELIVERED)

    @classmethod
    def _cleanup_ticket_worktrees(cls, ticket: "Ticket", result: SyncResult) -> None:
        """Free worktrees when the project board moves a ticket to Done.

        Squash-merge-aware cleanup via :func:`cleanup_worktree`. Worktrees whose
        branches carry genuinely-unpushed work are kept (the RuntimeError path);
        the user resolves those by running ``t3 teatree workspace clean-all``.
        """
        from teatree.core.models import Worktree  # noqa: PLC0415 — deferred: ORM import needs the app registry

        for worktree in Worktree.objects.filter(ticket=ticket):
            try:
                cleanup_result = cleanup_worktree(worktree)
                result.worktrees_cleaned += 1
                result.errors.extend(cleanup_result.errors)
            except WorktreeBusyError as exc:
                logger.info("Keeping worktree %s (live work): %s", worktree.repo_path, exc)
            except RuntimeError as exc:
                logger.info("Keeping worktree %s (unpushed work): %s", worktree.repo_path, exc)
            except Exception as exc:
                logger.exception("Failed to clean worktree %s", worktree.repo_path)
                result.errors.append(f"Worktree cleanup failed for {worktree.repo_path} ({worktree.branch}): {exc}")

    @classmethod
    def _fetch_reviewer_prs(cls, token: str) -> list[dict[str, str]]:
        gh_bin = shutil.which("gh")
        if not gh_bin:
            return []

        out = run_allowed_to_fail(
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
            env={**os.environ, "GH_TOKEN": token},
            expected_codes=None,
            timeout=30,
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
        except Exception as exc:  # noqa: BLE001 — a reviewer-PR fetch failure is recorded, never crashes the sync
            result.errors.append(f"GitHub reviewer PR fetch failed: {exc}")
            return

        existing: list[dict[str, str]] = cache.get(PENDING_REVIEWS_CACHE_KEY) or []
        existing_urls = {r["url"] for r in existing}
        merged = [*existing, *(r for r in reviews if r["url"] not in existing_urls)]
        cache.set(PENDING_REVIEWS_CACHE_KEY, merged, timeout=None)
        result.reviews_synced += len(reviews)
