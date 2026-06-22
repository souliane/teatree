from typing import cast

from django_typer.management import TyperCommand, command

from teatree.core.backend_factory import code_host_from_overlay
from teatree.core.models import Task, Ticket
from teatree.core.overlay_loader import get_overlay
from teatree.types import ConflictedMR, RawAPIDict
from teatree.url_classify import pr_ref


def _str_field(data: RawAPIDict, *names: str) -> str:
    for name in names:
        value = data.get(name)
        if isinstance(value, str):
            return value
    return ""


def _int_field(data: RawAPIDict, *names: str) -> int:
    for name in names:
        value = data.get(name)
        if isinstance(value, int):
            return value
    return 0


def _is_draft(pr: RawAPIDict) -> bool:
    return bool(pr.get("draft") or pr.get("work_in_progress"))


def _repo_slug(pr: RawAPIDict) -> str:
    """Best-effort ``owner/name`` for a PR/MR across GitLab and GitHub shapes."""
    references = pr.get("references")
    if isinstance(references, dict):
        full = cast("RawAPIDict", references).get("full")
        if isinstance(full, str) and full:
            return full.split("!", 1)[0].split("#", 1)[0]
    repository_url = _str_field(pr, "repository_url")
    if "/repos/" in repository_url:
        return repository_url.split("/repos/", 1)[-1]
    url = _str_field(pr, "web_url", "html_url")
    ref = pr_ref(url) if url else None
    return ref.slug if ref is not None else ""


class Command(TyperCommand):
    @command()
    def refresh(self) -> dict[str, int]:
        return {
            "tickets": Ticket.objects.count(),
            "tasks": Task.objects.count(),
            "open_tasks": Task.objects.exclude(status=Task.Status.COMPLETED).count(),
        }

    @command()
    def sync(self) -> dict[str, int | list[str] | list[dict[str, int | str]]]:
        from teatree.core.sync import sync_followup  # noqa: PLC0415

        result = sync_followup()
        self._warn_conflicted_mrs(result.conflicted_mrs)
        return {
            "prs_found": result.prs_found,
            "tickets_created": result.tickets_created,
            "tickets_updated": result.tickets_updated,
            "worktrees_cleaned": result.worktrees_cleaned,
            "errors": result.errors,
            "conflicted_mrs": [c.to_dict() for c in result.conflicted_mrs],
        }

    def _warn_conflicted_mrs(self, conflicted: list[ConflictedMR]) -> None:
        """Surface conflicted open authored MRs LOUDLY, never buried like errors.

        A conflicted MR sits invisibly until someone resolves it, and re-arises
        as master advances — so the sweep prints a clearly-visible WARNING
        block naming each one. Detection only: resolution stays an explicit,
        separate action (#78).
        """
        if not conflicted:
            return
        count = len(conflicted)
        plural = "s" if count != 1 else ""
        self.stdout.write("")
        self.stdout.write(f"{'=' * 64}")
        self.stdout.write(f"WARNING: {count} open MR{plural} in merge conflict — resolve before merge:")
        self.stdout.write(f"{'=' * 64}")
        for mr in conflicted:
            self.stdout.write(f"  CONFLICT  !{mr.iid}  {mr.repo}  {mr.title}")
            self.stdout.write(f"            {mr.web_url}")
        self.stdout.write(f"{'=' * 64}")

    @command(name="discover-mrs")
    def discover_mrs(self) -> RawAPIDict:
        """List the user's open, non-draft PRs/MRs awaiting a review request.

        Backs ``t3 review-request discover`` (BLUEPRINT.md §10.1). Mirrors
        ``glab api /merge_requests?scope=created_by_me&state=opened``
        filtered to non-draft MRs; each entry carries ``repo``, ``iid``,
        ``title`` and ``url`` so the result is suitable for the
        review-request batch ping or a human paste into Slack.
        """
        host = code_host_from_overlay()
        if host is None:
            return {"error": "No code host configured (check overlay tokens)"}

        author = get_overlay().config.get_gitlab_username() or host.current_user()
        if not author:
            return {"error": "Could not resolve author username — set <host>_username in ~/.teatree.toml"}

        mrs = [
            self._with_review_status(
                {
                    "repo": _repo_slug(pr),
                    "iid": _int_field(pr, "iid", "number"),
                    "title": _str_field(pr, "title"),
                    "url": _str_field(pr, "web_url", "html_url"),
                }
            )
            for pr in host.list_my_prs(author=author)
            if not _is_draft(pr)
        ]
        return {"author": author, "count": len(mrs), "mrs": mrs}

    @staticmethod
    def _with_review_status(mr: RawAPIDict) -> RawAPIDict:
        """Annotate an MR with a LIVE-verified review-request status (#1084).

        ``review_already_requested`` / ``review_permalink`` come from a
        recency-bounded read of the review channel (read-token ==
        post-token) so ``review-request discover`` reflects reality, not a
        stale DB. Fails open: an unconfigured channel or a slow/failed
        read leaves the MR unannotated rather than wedging discovery.
        """
        from teatree.core.gates.review_request_guard import reconcile_out_of_band, resolve_guard_target  # noqa: PLC0415

        url = mr.get("url")
        if not isinstance(url, str) or not url:
            return mr
        target = resolve_guard_target()
        if target is None:
            return mr
        permalink = reconcile_out_of_band(mr_url=url, target=target)
        mr["review_already_requested"] = bool(permalink)
        mr["review_permalink"] = permalink
        return mr

    @command()
    def remind(self) -> list[int]:
        return list(
            Task.objects.filter(
                execution_target=Task.ExecutionTarget.INTERACTIVE,
                status=Task.Status.PENDING,
            )
            .order_by("pk")
            .values_list("id", flat=True),
        )
