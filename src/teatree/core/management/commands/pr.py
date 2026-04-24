"""Pull request helpers: gate-validate then enqueue ship transition.

The actual push + MR creation lives in ``ShipExecutor`` (BLUEPRINT §4) and runs
inside the ``execute_ship`` task. This command is a deterministic CLI wrapper:
it runs the deterministic gates, calls ``ticket.ship()`` to enter SHIPPED, and
returns the MR URL once the worker completes.
"""

import re
from typing import TypedDict, cast

from django.db import transaction
from django_typer.management import TyperCommand, command

from teatree import visual_qa
from teatree.core.backend_factory import code_host_from_overlay, get_issue_tracker
from teatree.core.models import Ticket, Worktree
from teatree.core.models.types import TicketExtra, VisualQASummary
from teatree.core.overlay_loader import get_overlay
from teatree.core.runners.ship import overlay_mr_labels, sanitize_close_keywords
from teatree.utils import git


class VisualQAGateFailure(TypedDict):
    allowed: bool
    error: str
    visual_qa: VisualQASummary
    report_markdown: str
    hint: str


class ShipDryRun(TypedDict):
    dry_run: bool
    repo: str
    branch: str
    title: str
    description: str
    labels: list[str]


class MrValidationError(TypedDict):
    error: str
    details: list[str]


class WorktreeMissingError(TypedDict):
    error: str


class ShipEnqueued(TypedDict):
    ticket_id: int
    state: str


class ShippingGateFailure(TypedDict):
    allowed: bool
    error: str
    missing: list[str]
    hint: str


_IMAGE_URL_RE = re.compile(r"!\[([^\]]*)\]\((/uploads/[^\)]+)\)")
_EXTERNAL_LINK_RE = re.compile(r"https?://(?:www\.)?(?:notion\.so|linear\.app|jira\.\S+)/\S+")


def _ship_preview(ticket: Ticket, worktree: Worktree) -> tuple[str, str, str]:
    """Return ``(repo_path, title, description)`` previewed from the last commit."""
    repo_path = (worktree.extra or {}).get("worktree_path", "") or worktree.repo_path
    subject, body = git.last_commit_message(repo=repo_path)
    title = subject or f"Resolve {ticket.issue_url}"
    description = sanitize_close_keywords(body, close_ticket=get_overlay().config.mr_close_ticket)
    return repo_path, title, description


def _ship_dry_run(ticket: Ticket, worktree: Worktree) -> ShipDryRun:
    repo_path, title, description = _ship_preview(ticket, worktree)
    return ShipDryRun(
        dry_run=True,
        repo=repo_path,
        branch=worktree.branch,
        title=title,
        description=description,
        labels=overlay_mr_labels(),
    )


def _validate_mr_metadata(ticket: Ticket, worktree: Worktree) -> MrValidationError | None:
    _, title, description = _ship_preview(ticket, worktree)
    validation = get_overlay().metadata.validate_mr(title, description)
    if validation["errors"]:
        return MrValidationError(error="MR validation failed", details=validation["errors"])
    return None


def _check_shipping_gate(ticket: Ticket) -> ShippingGateFailure | None:
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
        return ShippingGateFailure(
            allowed=False,
            error=f"Gate check failed: {exc}",
            missing=missing,
            hint="Spawn a review sub-agent to satisfy the reviewing gate, then retry.",
        )
    return None


def _resolve_base_url(worktree: Worktree | None) -> str:
    """Return the frontend URL recorded by ``Worktree.verify()``.

    Prefers ``urls['frontend']`` (what users browse) over ``urls['backend']``.
    Falls back to ``http://127.0.0.1:8000`` when no URLs are recorded yet.
    """
    if worktree is None:
        return "http://127.0.0.1:8000"
    urls = worktree.get_extra().get("urls", {})
    return urls.get("frontend") or urls.get("backend") or "http://127.0.0.1:8000"


def _run_visual_qa_gate(ticket: Ticket, *, skip_reason: str = "") -> VisualQAGateFailure | None:
    """Run the pre-push browser sanity gate before MR creation.

    Records a JSON summary on ``ticket.extra['visual_qa']`` when the gate
    actually ran (i.e. not skipped for env/flag reasons) so the result
    survives in the FSM history.  Returns an error dict when blocking
    findings are present so the caller can refuse MR creation, or
    ``None`` when the gate passes / is skipped.
    """
    worktree = ticket.worktrees.first()  # ty: ignore[unresolved-attribute]
    repo_path = worktree.repo_path if worktree else "."
    base_url = _resolve_base_url(worktree)

    overlay = get_overlay()
    diff = visual_qa.changed_files(repo=repo_path)
    report = visual_qa.evaluate(diff=diff, overlay=overlay, base_url=base_url, skip_reason=skip_reason)

    # Only persist when the gate produced a meaningful signal — skipping a
    # no-op run keeps the FSM history readable.
    if report.pages or report.has_errors:
        extra = cast("TicketExtra", ticket.extra or {})
        extra["visual_qa"] = report.summary()
        ticket.extra = extra
        ticket.save(update_fields=["extra"])

    if not report.has_errors:
        return None
    return VisualQAGateFailure(
        allowed=False,
        error=f"Visual QA found {report.total_errors} blocking finding(s).",
        visual_qa=report.summary(),
        report_markdown=visual_qa.format_report(report),
        hint="Fix the findings, or pass --skip-visual-qa <reason> to bypass.",
    )


class Command(TyperCommand):
    @command()
    def create(
        self,
        ticket_id: int,
        *,
        title: str = "",
        dry_run: bool = False,
        skip_validation: bool = False,
        skip_visual_qa: str = "",
    ) -> (
        ShipEnqueued | ShipDryRun | MrValidationError | VisualQAGateFailure | ShippingGateFailure | WorktreeMissingError
    ):
        """Validate ship gates and trigger the ship transition.

        On success the ``execute_ship`` worker pushes the branch, opens the MR,
        and advances ``SHIPPED → IN_REVIEW``. The return value reports the MR
        URL once the worker completes (synchronous in interactive mode).

        ``--title`` overrides the MR title (default: last commit subject).
        Stored on ``ticket.extra['mr_title_override']`` so the worker reads it.
        """
        ticket = Ticket.objects.get(pk=ticket_id)
        worktree = ticket.worktrees.first()
        if worktree is None:
            return WorktreeMissingError(error="ticket has no worktree")

        if not skip_validation:
            gate_error = _check_shipping_gate(ticket)
            if gate_error:
                return gate_error
            visual_qa_error = _run_visual_qa_gate(ticket, skip_reason=skip_visual_qa)
            if visual_qa_error:
                return visual_qa_error
            validation_error = _validate_mr_metadata(ticket, worktree)
            if validation_error:
                return validation_error

        if dry_run:
            return _ship_dry_run(ticket, worktree)

        with transaction.atomic():
            if title:
                extra = cast("TicketExtra", ticket.extra or {})
                extra["mr_title_override"] = title
                ticket.extra = extra
            ticket.ship()
            ticket.save()
        return ShipEnqueued(ticket_id=int(ticket.pk), state=str(ticket.state))

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
