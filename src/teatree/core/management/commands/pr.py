"""Pull request helpers: create, check gates, fetch issue, detect tenant."""

from collections.abc import Iterable

from django_typer.management import TyperCommand, command

from teatree.backends.loader import get_code_host, get_issue_tracker
from teatree.core.models import Ticket
from teatree.core.overlay_loader import get_overlay


class Command(TyperCommand):
    @command()
    def create(
        self,
        ticket_id: int,
        repo: str = "",
        title: str = "",
        description: str = "",
    ) -> dict[str, object]:
        """Create a merge request for the ticket's branch."""
        ticket = Ticket.objects.get(pk=ticket_id)
        host = get_code_host()
        if host is None:
            return {"error": "No code host configured (check overlay GitLab token)"}

        worktree = ticket.worktrees.first()
        branch = worktree.branch if worktree else f"ticket-{ticket.ticket_number}"
        repo_path = repo or (worktree.repo_path if worktree else "")

        overlay = get_overlay()
        validation = overlay.metadata.validate_mr(title, description)
        if validation["errors"]:
            return {"error": "MR validation failed", "details": validation["errors"]}

        return host.create_pr(
            repo=repo_path,
            branch=branch,
            title=title or f"Resolve {ticket.issue_url}",
            description=description,
            labels=_mr_auto_labels() or None,
        )

    @command(name="check-gates")
    def check_gates(self, ticket_id: int, target_phase: str = "shipping") -> dict[str, object]:
        """Check whether session gates allow a phase transition."""
        ticket = Ticket.objects.get(pk=ticket_id)
        session = ticket.sessions.order_by("-pk").first()
        if session is None:
            return {"allowed": False, "reason": "No active session"}
        try:
            session.check_gate(target_phase)
        except (ValueError, KeyError) as exc:
            return {"allowed": False, "reason": str(exc)}
        else:
            return {"allowed": True, "target_phase": target_phase}

    @command(name="fetch-issue")
    def fetch_issue(self, issue_url: str) -> dict[str, object]:
        """Fetch issue details from the configured tracker."""
        tracker = get_issue_tracker()
        if tracker is None:
            return {"error": "No issue tracker configured"}
        return tracker.get_issue(issue_url)

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
        host = get_code_host()
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
    raw = get_overlay().config.get_mr_auto_labels()
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, Iterable):
        values = [str(value) for value in raw]
    else:
        return []

    return [value.strip() for value in values if value.strip()]
