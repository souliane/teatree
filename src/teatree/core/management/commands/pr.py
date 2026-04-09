"""Pull request helpers: create, check gates, fetch issue, detect tenant."""

import re
from collections.abc import Iterable
from typing import cast

from django_typer.management import TyperCommand, command

from teatree.core.backend_factory import code_host_from_overlay, get_issue_tracker
from teatree.core.models import Ticket
from teatree.core.overlay_loader import get_overlay
from teatree.utils import git

_IMAGE_URL_RE = re.compile(r"!\[([^\]]*)\]\((/uploads/[^\)]+)\)")
_EXTERNAL_LINK_RE = re.compile(r"https?://(?:www\.)?(?:notion\.so|linear\.app|jira\.\S+)/\S+")


def _current_user() -> str:
    """Return the git user name for MR auto-assignment."""
    return git.config_value(key="user.name")


def _last_commit_message(cwd: str) -> tuple[str, str]:
    """Return ``(subject, body)`` from the last git commit in *cwd*."""
    return git.last_commit_message(repo=cwd)


def _check_shipping_gate(ticket: Ticket) -> dict[str, object] | None:
    """Return an error dict if the ticket hasn't passed the review gate.

    Returns structured JSON with ``missing`` phases so the calling agent
    can spawn a sub-agent to satisfy the gate rather than failing outright.
    """
    from teatree.core.models.errors import QualityGateError  # noqa: PLC0415

    session = ticket.sessions.order_by("-pk").first()  # ty: ignore[unresolved-attribute]
    if session is None:
        return None
    try:
        session.check_gate("shipping")
    except QualityGateError as exc:
        visited = session.visited_phases or []
        required = session._REQUIRED_PHASES.get("shipping", [])  # noqa: SLF001
        missing = [p for p in required if p not in visited]
        return {
            "allowed": False,
            "error": f"Gate check failed: {exc}",
            "missing": missing,
            "hint": "Spawn a review sub-agent to satisfy the reviewing gate, then retry.",
        }
    return None


class Command(TyperCommand):
    @command()
    def create(  # noqa: PLR0913
        self,
        ticket_id: int,
        repo: str = "",
        title: str = "",
        description: str = "",
        *,
        dry_run: bool = False,
        skip_validation: bool = False,
    ) -> dict[str, object]:
        """Create a merge request for the ticket's branch."""
        ticket = Ticket.objects.get(pk=ticket_id)
        host = code_host_from_overlay()
        if host is None:
            return {"error": "No code host configured (check overlay GitLab token)"}

        worktree = ticket.worktrees.first()
        branch = worktree.branch if worktree else f"ticket-{ticket.ticket_number}"
        repo_path = repo or (worktree.repo_path if worktree else "")

        # Auto-fill title/description from the last commit message
        if not title or not description:
            wt_path = (worktree.extra or {}).get("worktree_path", "") if worktree else ""
            commit_subject, commit_body = _last_commit_message(wt_path or repo_path)
            if not title:
                title = commit_subject or f"Resolve {ticket.issue_url}"
            if not description:
                description = commit_body

        overlay = get_overlay()

        if not skip_validation:
            gate_error = _check_shipping_gate(ticket)
            if gate_error:
                return gate_error
            validation = overlay.metadata.validate_mr(title, description)
            if validation["errors"]:
                return {"error": "MR validation failed", "details": validation["errors"]}

        if dry_run:
            return {
                "dry_run": True,
                "repo": repo_path,
                "branch": branch,
                "title": title,
                "description": description,
                "labels": _mr_auto_labels(),
            }

        assignee = _current_user()
        return host.create_pr(
            repo=repo_path,
            branch=branch,
            title=title,
            description=description,
            labels=_mr_auto_labels() or None,
            assignee=assignee,
        )

    @command(name="check-gates")
    def check_gates(self, ticket_id: int, target_phase: str = "shipping") -> dict[str, object]:
        """Check whether session gates allow a phase transition."""
        from teatree.core.models.errors import QualityGateError  # noqa: PLC0415

        ticket = Ticket.objects.get(pk=ticket_id)
        session = ticket.sessions.order_by("-pk").first()
        if session is None:
            return {"allowed": False, "reason": "No active session", "missing": []}
        try:
            session.check_gate(target_phase)
        except QualityGateError:
            visited = session.visited_phases or []
            required = session._REQUIRED_PHASES.get(target_phase, [])  # noqa: SLF001
            missing = [p for p in required if p not in visited]
            return {"allowed": False, "missing": missing, "reason": f"{target_phase} requires: {', '.join(missing)}"}
        except (ValueError, KeyError) as exc:
            return {"allowed": False, "reason": str(exc), "missing": []}
        else:
            return {"allowed": True, "target_phase": target_phase}

    @command(name="fetch-issue")
    def fetch_issue(self, issue_url: str) -> dict[str, object]:
        """Fetch issue details with embedded image URLs and external links."""
        tracker = get_issue_tracker()
        if tracker is None:
            return {"error": "No issue tracker configured"}
        issue = tracker.get_issue(issue_url)
        description = str(issue.get("description", ""))

        # Extract embedded image paths (GitLab /uploads/ references)
        images = _IMAGE_URL_RE.findall(description)
        if images:
            issue["_embedded_images"] = [{"alt": alt, "path": path} for alt, path in images]

        # Extract external links (Notion, Linear, Jira)
        external_links = list(set(_EXTERNAL_LINK_RE.findall(description)))
        if external_links:
            issue["_external_links"] = external_links

        # Extract comments if available
        comments = issue.get("comments") or issue.get("notes")
        if isinstance(comments, list):
            for item in comments:
                if not isinstance(item, dict):
                    continue
                comment = cast("dict[str, object]", item)
                body = str(comment.get("body", ""))
                comment_images = _IMAGE_URL_RE.findall(body)
                if comment_images:
                    comment["_embedded_images"] = [{"alt": alt, "path": path} for alt, path in comment_images]

        return issue

    @command(name="detect-tenant")
    def detect_tenant(self) -> str:
        """Detect the current tenant variant from the overlay."""
        return get_overlay().metadata.detect_variant()

    @command(name="post-evidence")
    def post_evidence(
        self,
        mr_iid: int,
        repo: str = "",
        title: str = "Test Plan",
        body: str = "",
        files: list[str] | None = None,
    ) -> dict[str, object]:
        """Post test evidence as an MR comment. Uploads files and updates existing notes.

        Files (screenshots, videos) are uploaded and embedded as ``![name](url)`` in the body.
        If an existing note contains ``## Test Plan``, it is updated instead of creating a new one.
        """
        host = code_host_from_overlay()
        if host is None:
            return {"error": "No code host configured (check overlay GitLab token)"}

        repo_path = repo or get_overlay().metadata.get_ci_project_path()

        # Upload files and build markdown embeds
        embeds: list[str] = []
        for filepath in files or []:
            result = host.upload_file(repo=repo_path, filepath=filepath)
            md = result.get("markdown", "")
            if md:
                embeds.append(str(md))
                self.stdout.write(f"  Uploaded: {filepath}")

        # Build note body
        embed_section = "\n\n".join(embeds)
        note_body = f"## {title}\n\n{body}" if body else f"## {title}\n\n_No details provided._"
        if embed_section:
            note_body += f"\n\n{embed_section}"

        # Find existing test plan note to update
        existing_notes = host.list_mr_notes(repo=repo_path, mr_iid=mr_iid)
        marker = "## Test Plan"
        existing_note = next(
            (n for n in existing_notes if marker in str(n.get("body", "")) and not n.get("system")),
            None,
        )

        if existing_note:
            note_id = int(str(existing_note["id"]))
            self.stdout.write(f"  Updating existing note {note_id}")
            return host.update_mr_note(repo=repo_path, mr_iid=mr_iid, note_id=note_id, body=note_body)

        return host.post_mr_note(repo=repo_path, mr_iid=mr_iid, body=note_body)


def _mr_auto_labels() -> list[str]:
    raw = get_overlay().config.mr_auto_labels
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, Iterable):
        values = [str(value) for value in raw]
    else:
        return []

    return [value.strip() for value in values if value.strip()]
