"""Pull request helpers: gate-validate then enqueue ship transition.

The actual push + PR creation lives in ``ShipExecutor`` (BLUEPRINT §4) and runs
inside the ``execute_ship`` task. This command is a deterministic CLI wrapper:
it runs the deterministic gates, calls ``ticket.ship()`` to enter SHIPPED, and
returns the PR URL once the worker completes.
"""

import re
from typing import TypedDict, cast

from django.db import transaction
from django_fsm import TransitionNotAllowed
from django_typer.management import TyperCommand, command

from teatree import visual_qa
from teatree.backends.protocols import PullRequestSpec
from teatree.core.backend_factory import code_host_from_overlay
from teatree.core.management.commands._ship_fsm import reconcile_fsm_for_ship
from teatree.core.models import Session, Ticket, Worktree
from teatree.core.models.types import TicketExtra, VisualQASummary
from teatree.core.orphan_guard import BranchStatus, classify_branch
from teatree.core.overlay_loader import get_overlay
from teatree.core.public_identity import MergeResult
from teatree.core.runners.ship import overlay_pr_labels, sanitize_close_keywords
from teatree.types import RawAPIDict
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


class PrValidationError(TypedDict):
    error: str
    details: list[str]


class WorktreeMissingError(TypedDict):
    error: str


class ShipEnqueued(TypedDict):
    ticket_id: int
    state: str
    queued: bool
    warning: str


class ShipExecuted(TypedDict):
    ticket_id: int
    state: str
    synced: bool
    ok: bool
    detail: str


class ShippingGateFailure(TypedDict):
    allowed: bool
    error: str
    missing: list[str]
    hint: str


class EnsurePrResult(TypedDict, total=False):
    skipped: str
    branch: str
    url: str
    hint: str
    error: str


_IMAGE_URL_RE = re.compile(r"!\[([^\]]*)\]\((/uploads/[^\)]+)\)")
_EXTERNAL_LINK_RE = re.compile(r"https?://(?:www\.)?(?:notion\.so|linear\.app|jira\.\S+)/\S+")
_REMOTE_HOST_RE = re.compile(r"^(?:git@[^:]+:|https?://[^/]+/|ssh://[^/]+/|git://[^/]+/)")


def _slug_from_remote(remote_url: str) -> str:
    """Extract the ``org/repo`` (or ``ns/group/repo``) slug from a git remote URL."""
    if not remote_url:
        return ""
    return _REMOTE_HOST_RE.sub("", remote_url.strip()).removesuffix(".git")


def _ship_preview(ticket: Ticket, worktree: Worktree) -> tuple[str, str, str]:
    """Return ``(repo_path, title, description)`` previewed from the last commit.

    Invariant (MR title/description divergence guard): the description's
    first line is built from the *same* string as the title, so they can
    never diverge by construction. A diverged title vs. description-first-
    line is exactly what blocks the release-notes pipeline; building the
    first line from ``title`` (not a separately-derived ``subject``) makes
    that regression impossible.
    """
    repo_path = (worktree.extra or {}).get("worktree_path", "") or worktree.repo_path
    subject, body = git.last_commit_message(repo=repo_path)
    close_ticket = get_overlay().config.mr_close_ticket
    # Sanitize the TITLE first, then build the description's first line from
    # that exact sanitized string. Applying close-keyword sanitization only
    # to the description (the old behaviour) silently diverged it from the
    # title whenever the title carried a close-keyword (e.g. the "Resolve
    # <url>" fallback, or a "fix: resolve X" subject) — the title/
    # description divergence class. Sanitizing the title and reusing it
    # makes the first line == title by construction.
    title = sanitize_close_keywords(subject or f"Resolve {ticket.issue_url}", close_ticket=close_ticket)
    raw_body = sanitize_close_keywords(body, close_ticket=close_ticket) if body else ""
    description = f"{title}\n\n{raw_body}" if raw_body else title
    return repo_path, title, description


def _ship_dry_run(ticket: Ticket, worktree: Worktree) -> ShipDryRun:
    repo_path, title, description = _ship_preview(ticket, worktree)
    return ShipDryRun(
        dry_run=True,
        repo=repo_path,
        branch=worktree.branch,
        title=title,
        description=description,
        labels=overlay_pr_labels(),
    )


def _validate_pr_metadata(ticket: Ticket, worktree: Worktree) -> PrValidationError | None:
    _, title, description = _ship_preview(ticket, worktree)
    validation = get_overlay().metadata.validate_pr(title, description)
    if validation["errors"]:
        return PrValidationError(error="PR validation failed", details=validation["errors"])
    return None


def _check_shipping_gate(ticket: Ticket) -> ShippingGateFailure | None:
    """Reconcile ``ticket.state`` from the session, or block with missing phases.

    ``Session.visited_phases`` is the single source of truth (#694). When the
    required phases are present this auto-walks the FSM to REVIEWED so
    ``ticket.ship()`` is legal — the gate and ``ticket.state`` can no longer
    disagree. When phases are missing it returns structured JSON with the
    exact ``missing`` list so the calling agent can satisfy the gate rather
    than hitting a raw ``TransitionNotAllowed``.
    """
    from teatree.core.models.errors import QualityGateError  # noqa: PLC0415

    session = ticket.sessions.order_by("-pk").first()  # ty: ignore[unresolved-attribute]
    if session is None:
        # No session => no attested work; nothing to reconcile. Returning
        # ``None`` here would let ``ticket.ship()`` raise a raw
        # ``TransitionNotAllowed`` from a non-REVIEWED state, breaking the
        # "pr create never raises a raw TransitionNotAllowed" invariant.
        required = Session._REQUIRED_PHASES.get("shipping", [])  # noqa: SLF001
        return ShippingGateFailure(
            allowed=False,
            error="No session: no attested work to reconcile.",
            missing=list(required),
            hint="Run the work through the loop (or `lifecycle visit-phase`) so the phases are recorded, then retry.",
        )
    try:
        # Single source of truth = the union of phase records across ALL
        # the ticket's sessions, not just the latest (#694). FSM-advancing
        # `visit-phase` forks fresh sessions by design; the required phases
        # are legitimately scattered across the ticket lifecycle.
        session.check_gate_across_ticket("shipping")
    except QualityGateError as exc:
        visited, _ = ticket.aggregate_phase_records()
        required = Session._REQUIRED_PHASES.get("shipping", [])  # noqa: SLF001
        missing = [p for p in required if p not in visited]
        return ShippingGateFailure(
            allowed=False,
            error=f"Gate check failed: {exc}",
            missing=missing,
            hint="Spawn a review sub-agent to satisfy the reviewing gate, then retry.",
        )

    # Gate passed -> the work is attested. Reconcile the FSM from the single
    # source of truth so ``ship()`` (source [REVIEWED, SHIPPED]) is legal.
    reconcile_fsm_for_ship(ticket)
    return None


def _do_ship_transition(ticket: Ticket, title: str) -> ShippingGateFailure | None:
    """Run the ``ship()`` FSM transition; return a gate failure or ``None``.

    Invariant (#694): ``pr create`` never raises a raw
    ``TransitionNotAllowed``. Since #748 the ``--skip-validation`` path
    runs ``reconcile_fsm_for_ship`` too (it is the user-authorized
    attestation substitute, so the FSM follows the authorization), so
    ``ship()`` is normally legal here; this ``try`` remains the backstop
    for any residual illegal hop (e.g. a state the reconcile no-ops past)
    so the failure is reported as the same structured shape the gate-fail
    path returns rather than raised. ``ship()`` schedules
    ``execute_ship.enqueue`` via ``transaction.on_commit``.
    """
    try:
        with transaction.atomic():
            if title:
                extra = cast("TicketExtra", ticket.extra or {})
                extra["pr_title_override"] = title
                ticket.extra = extra
            ticket.ship()
            ticket.save()
    except TransitionNotAllowed:
        return ShippingGateFailure(
            allowed=False,
            error=f"Cannot ship from state '{ticket.state}': FSM not in REVIEWED.",
            missing=[],
            hint="Drop --skip-validation so the gate can reconcile the FSM, or record the missing phases.",
        )
    return None


def _enqueue_ship(ticket: Ticket, title: str) -> ShipEnqueued | ShippingGateFailure:
    """Async ship: enqueue ``execute_ship`` and warn it needs a worker.

    The push + PR are NOT performed here — they run in ``execute_ship``,
    which only fires when a worker drains the django-tasks queue. In a
    no-worker context (e.g. an interactive ``uv run`` invocation) the ship
    silently never completes; the explicit ``warning`` makes that visible
    instead of looking like a successful ship (#708). Use ``--sync`` to
    push + open the PR inline in this process.
    """
    failure = _do_ship_transition(ticket, title)
    if failure is not None:
        return failure
    return ShipEnqueued(
        ticket_id=int(ticket.pk),
        state=str(ticket.state),
        queued=True,
        warning=(
            "Ship was QUEUED, not performed. The branch push and PR creation "
            "run in the `execute_ship` task and will NOT complete until a "
            "worker drains the queue (`t3 <overlay> tasks work-next-sdk`). "
            "Re-run with `--sync` to push and open the PR inline now."
        ),
    )


def _ship_sync(ticket: Ticket, title: str) -> ShipExecuted | ShippingGateFailure:
    """Synchronous ship: run ``execute_ship`` inline in this process (#708).

    After the FSM transition commits, invoke the ship task synchronously
    via django-tasks' ``.call()`` so the push + PR happen before the
    command returns — no queue worker required. ``execute_ship`` is
    idempotent (re-checks state under ``select_for_update``), so the
    ``on_commit`` enqueue scheduled by ``ship()`` is a safe no-op if a
    worker later picks it up (state is no longer SHIPPED).
    """
    from teatree.core.tasks import execute_ship  # noqa: PLC0415

    failure = _do_ship_transition(ticket, title)
    if failure is not None:
        return failure
    result = execute_ship.call(int(ticket.pk))
    ticket.refresh_from_db()
    return ShipExecuted(
        ticket_id=int(ticket.pk),
        state=str(ticket.state),
        synced=True,
        ok=bool(result.get("ok", False)),
        detail=str(result.get("detail", "")),
    )


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
    """Run the pre-push browser sanity gate before PR creation.

    Records a JSON summary on ``ticket.extra['visual_qa']`` when the gate
    actually ran (i.e. not skipped for env/flag reasons) so the result
    survives in the FSM history.  Returns an error dict when blocking
    findings are present so the caller can refuse PR creation, or
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


def _run_ship_gates(
    ticket: Ticket,
    worktree: Worktree,
    *,
    skip_visual_qa: str = "",
) -> ShippingGateFailure | VisualQAGateFailure | PrValidationError | None:
    """Run the pre-ship gates in order; return the first failure or ``None``.

    Composed out of ``create`` so the command stays within the
    return-count gate and the gate sequence is independently testable.
    """
    gate_error = _check_shipping_gate(ticket)
    if gate_error is not None:
        return gate_error
    visual_qa_error = _run_visual_qa_gate(ticket, skip_reason=skip_visual_qa)
    if visual_qa_error is not None:
        return visual_qa_error
    return _validate_pr_metadata(ticket, worktree)


def _resolve_ticket(ref: str) -> Ticket:
    """Resolve a ticket by pk / issue number / issue URL.

    Thin wrapper over ``Ticket.objects.resolve`` — the shared resolver so
    ``pr create`` and ``lifecycle visit-phase`` accept the same identifier
    set (#694).
    """
    return Ticket.objects.resolve(ref)


class Command(TyperCommand):
    @command()
    # PLR0913: this signature IS the CLI contract — django-typer derives
    # --title/--dry-run/--skip-validation/--skip-visual-qa/--sync by
    # introspecting these kwargs; bundling them into a dataclass would delete
    # the flags. Same targeted-noqa-for-framework-reality pattern as the
    # repo's PLC0415 (import-in-function) and SLF001 (framework internals).
    def create(  # noqa: PLR0913
        self,
        ticket_id: str,
        *,
        title: str = "",
        dry_run: bool = False,
        skip_validation: bool = False,
        skip_visual_qa: str = "",
        sync: bool = False,
    ) -> (
        ShipEnqueued
        | ShipExecuted
        | ShipDryRun
        | PrValidationError
        | VisualQAGateFailure
        | ShippingGateFailure
        | WorktreeMissingError
    ):
        """Validate ship gates and trigger the ship transition.

        Default (async): the ship is *queued* — ``execute_ship`` pushes the
        branch and opens the PR only when a worker drains the django-tasks
        queue. The result carries an explicit ``warning`` so a no-worker
        context does not look like a completed ship (#708).

        ``--sync``: run ``execute_ship`` inline in this process so the push
        and PR happen before the command returns — no worker required. Use
        this for interactive / ``uv run`` invocations where nothing is
        draining the queue.

        ``ticket_id`` accepts the internal DB pk, the full issue URL, or the
        bare issue number (resolved against ``Ticket.issue_url``).

        ``--title`` overrides the PR title (default: last commit subject).
        Stored on ``ticket.extra['pr_title_override']`` so the ship reads it.
        """
        ticket = _resolve_ticket(ticket_id)
        worktree = ticket.worktrees.first()  # ty: ignore[unresolved-attribute]
        if worktree is None:
            return WorktreeMissingError(error="ticket has no worktree")

        if not skip_validation:
            gate_failure = _run_ship_gates(ticket, worktree, skip_visual_qa=skip_visual_qa)
            if gate_failure is not None:
                return gate_failure
        else:
            # --skip-validation is the user-authorized attestation
            # substitute (the gate-fixer bootstrap, /t3:ship §5 #2). The
            # FSM must follow the authorization or ship() is structurally
            # impossible from a non-REVIEWED state (#748).
            reconcile_fsm_for_ship(ticket)

        if dry_run:
            return _ship_dry_run(ticket, worktree)
        if sync:
            return _ship_sync(ticket, title)
        return _enqueue_ship(ticket, title)

    @command(name="ensure-pr")
    def ensure_pr(
        self,
        branch: str = "",
        repo: str = "",
    ) -> EnsurePrResult:
        """Create a PR for an orphan branch (idempotent, no-op when a PR already exists).

        An orphan is a branch with commits not on ``origin/main`` (after
        subject-match + tree-equality checks) and no open PR. When this
        runs inside a git pre-push hook for a *first* push, the branch is not
        yet on the remote — creating the PR is deferred with a warning so the
        push proceeds and the agent can re-run this command afterwards.
        """
        repo_path = repo or "."
        branch_name = branch or git.current_branch(repo=repo_path)
        if not branch_name or branch_name in {"HEAD", "main", "master"}:
            return EnsurePrResult(skipped="not on a feature branch", branch=branch_name)

        report = classify_branch(repo_path, branch_name)

        if report.status is BranchStatus.SYNCED:
            return EnsurePrResult(skipped="branch synced to origin/main", branch=branch_name)
        if report.status is BranchStatus.OPEN_PR:
            return EnsurePrResult(skipped="open PR exists", branch=branch_name, url=report.open_pr_url)
        if report.status is BranchStatus.UNPUSHED_ORPHAN:
            return EnsurePrResult(
                skipped="branch not on remote yet — re-run after push completes",
                branch=branch_name,
                hint=f"t3 <overlay> pr ensure-pr --branch {branch_name}",
            )

        host = code_host_from_overlay()
        if host is None:
            return EnsurePrResult(error="no code host configured")

        commit_subject, commit_body = git.last_commit_message(repo=repo_path)
        title = commit_subject or f"WIP: {branch_name}"
        raw_description = (
            f"{commit_subject}\n\n{commit_body}"
            if commit_subject and commit_body
            else (commit_subject or commit_body or f"PR auto-created to track branch `{branch_name}`.")
        )
        description = sanitize_close_keywords(raw_description, close_ticket=get_overlay().config.mr_close_ticket)

        remote = git.remote_url(repo=repo_path)
        repo_slug = _slug_from_remote(remote)
        assignee = host.current_user() or git.config_value(key="user.name")

        raw = host.create_pr(
            PullRequestSpec(
                repo=repo_slug,
                branch=branch_name,
                title=title,
                description=description,
                labels=overlay_pr_labels(),
                assignee=assignee,
                draft=False,
            ),
        )
        return EnsurePrResult(branch=branch_name, url=str(raw.get("url", raw.get("web_url", ""))))

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

    @command(name="merge")
    def merge(self, pr: int, slug: str, *, auto: bool = False) -> MergeResult:
        """Merge a PR with a deterministic author (#764, supersedes #762).

        The merge path the review-loop MUST use instead of raw
        ``gh pr merge --squash``. On public ``souliane/*`` it performs a
        LOCAL ``git merge --squash`` + ``git commit`` with the author and
        committer forced to the canonical noreply identity, then
        ``git push origin main`` — deterministic regardless of any GitHub
        account / git config. A push rejection (protected branch /
        non-fast-forward) stops with an error (no force-push); the landed
        commit author is then verified via ``gh api`` (fail-closed).
        Non-souliane / private remotes use the server-side ``gh pr
        merge`` path, unchanged.
        """
        from teatree.core.pr_merge import squash_merge_public  # noqa: PLC0415

        squash_merge_public(pr=pr, slug=slug, auto=auto)
        return MergeResult(merged=True, pr=pr, slug=slug, auto=auto)

    @command(name="fetch-issue")
    def fetch_issue(self, issue_url: str) -> dict[str, object]:
        """Fetch issue details with embedded image URLs and external links."""
        host = code_host_from_overlay()
        if host is None:
            return {"error": "No code host configured"}
        issue = host.get_issue(issue_url)
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

    @command(name="sweep")
    def sweep(self) -> RawAPIDict:
        """List all open PRs/MRs authored by the current user across the forge.

        Output is consumed by the ``/t3:sweeping-prs`` agent skill, which walks
        each PR sequentially: merges the default branch, fixes conflicts,
        monitors CI, and pushes — never rebases. The CLI itself only
        discovers; mutating actions live in the skill so the agent can
        prompt for non-default-base PRs and conflict resolution.
        """
        host = code_host_from_overlay()
        if host is None:
            return {"error": "No code host configured (check overlay tokens)"}

        author = get_overlay().config.get_gitlab_username() or host.current_user()
        if not author:
            return {"error": "Could not resolve author username — set <host>_username in ~/.teatree.toml"}

        prs = host.list_my_prs(author=author)
        return {"author": author, "count": len(prs), "prs": prs}

    @command(name="post-evidence")
    def post_evidence(
        self,
        mr_iid: int,
        repo: str = "",
        title: str = "Test Plan",
        body: str = "",
        files: list[str] | None = None,
    ) -> dict[str, object]:
        """Post test evidence as a PR comment. Uploads files and updates existing notes.

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
        existing_notes = host.list_pr_comments(repo=repo_path, pr_iid=mr_iid)
        marker = "## Test Plan"
        existing_note = next(
            (n for n in existing_notes if marker in str(n.get("body", "")) and not n.get("system")),
            None,
        )

        if existing_note:
            comment_id = int(str(existing_note["id"]))
            self.stdout.write(f"  Updating existing note {comment_id}")
            return host.update_pr_comment(repo=repo_path, pr_iid=mr_iid, comment_id=comment_id, body=note_body)

        return host.post_pr_comment(repo=repo_path, pr_iid=mr_iid, body=note_body)
