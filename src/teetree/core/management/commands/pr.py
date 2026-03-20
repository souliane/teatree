"""Pull request helpers: create, check gates, fetch issue, detect tenant."""

from collections.abc import Iterable

from django.conf import settings
from django_typer.management import TyperCommand, command

from teetree.backends.loader import get_code_host, get_issue_tracker
from teetree.core.models import Ticket
from teetree.core.overlay_loader import get_overlay


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
            return {"error": "No code host configured (TEATREE_CODE_HOST)"}

        worktree = ticket.worktrees.first()
        branch = worktree.branch if worktree else f"ticket-{ticket.ticket_number}"
        repo_path = repo or (worktree.repo_path if worktree else "")

        overlay = get_overlay()
        validation = overlay.validate_mr(title, description)
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
            return {"error": "No issue tracker configured (TEATREE_ISSUE_TRACKER)"}
        return tracker.get_issue(issue_url)

    @command(name="detect-tenant")
    def detect_tenant(self) -> str:
        """Detect the current tenant variant from the overlay."""
        return get_overlay().detect_variant()

    @command(name="post-evidence")
    def post_evidence(
        self,
        mr_iid: int,
        repo: str = "",
        title: str = "Test Evidence",
        body: str = "",
    ) -> dict[str, object]:
        """Post test evidence (screenshots, results) as an MR comment."""
        host = get_code_host()
        if host is None:
            return {"error": "No code host configured (TEATREE_CODE_HOST)"}

        repo_path = repo or get_overlay().get_ci_project_path()
        note_body = f"### {title}\n\n{body}" if body else f"### {title}\n\n_No details provided._"
        return host.post_mr_note(repo=repo_path, mr_iid=mr_iid, body=note_body)


def _mr_auto_labels() -> list[str]:
    raw = getattr(settings, "TEATREE_MR_AUTO_LABELS", [])
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, Iterable):
        values = [str(value) for value in raw]
    else:
        return []

    return [value.strip() for value in values if value.strip()]
