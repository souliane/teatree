"""Pull request helpers: gate-validate then enqueue ship transition.

The actual push + PR creation lives in ``ShipExecutor`` (BLUEPRINT §4) and runs
inside the ``execute_ship`` task. This command is a deterministic CLI wrapper:
it runs the deterministic gates, calls ``ticket.ship()`` to enter SHIPPED, and
returns the PR URL once the worker completes.
"""

import re
from typing import TypedDict, cast

from django_typer.management import TyperCommand, command

from teatree import visual_qa
from teatree.core.backend_factory import code_host_from_overlay
from teatree.core.db_anchor import assert_lifecycle_db_is_canonical
from teatree.core.management.commands._ensure_pr import EnsurePrResult, create_or_defer_pr
from teatree.core.management.commands._pr_preview import (
    PrValidationError,
    ShipDryRun,
    ship_dry_run,
    validate_pr_metadata,
)
from teatree.core.management.commands._ship_exec import (
    ShipEnqueued,
    ShipExecuted,
    ShippingGateFailure,
    _enqueue_ship,
    _ship_sync,
)
from teatree.core.management.commands._ship_fsm import reconcile_fsm_for_ship
from teatree.core.models import Session, Ticket, Worktree
from teatree.core.models.types import TicketExtra, VisualQASummary
from teatree.core.orphan_guard import BranchStatus, classify_branch
from teatree.core.overlay_loader import get_overlay
from teatree.core.public_identity import MergeResult
from teatree.core.runners.ship import resolve_ship_worktree
from teatree.types import RawAPIDict
from teatree.utils import git
from teatree.utils.run import CommandFailedError


class VisualQAGateFailure(TypedDict):
    allowed: bool
    error: str
    visual_qa: VisualQASummary
    report_markdown: str
    hint: str


# ShipDryRun / PrValidationError live in ``_pr_preview`` (re-exported below)
# so external importers of ``pr.ShipDryRun`` / ``pr.PrValidationError`` keep
# working after the ship-preview split.


class WorktreeMissingError(TypedDict):
    error: str


class NoCommitsAheadError(TypedDict):
    error: str
    branch: str
    base: str


# ShipEnqueued / ShipExecuted / ShippingGateFailure and the ship-execution
# helpers live in ``_ship_exec`` (extracted by concern, re-exported above)
# so external importers of ``pr.ShipExecuted`` etc. keep working and
# ``pr.py`` stays within the module-health LOC bar.

# EnsurePrResult lives in ``_ensure_pr`` (re-exported above) so external
# importers of ``pr.EnsurePrResult`` keep working after the ensure-pr split.


_IMAGE_URL_RE = re.compile(r"!\[([^\]]*)\]\((/uploads/[^\)]+)\)")
_EXTERNAL_LINK_RE = re.compile(r"https?://(?:www\.)?(?:notion\.so|linear\.app|jira\.\S+)/\S+")


def _assert_commits_ahead_of_base(worktree: Worktree) -> NoCommitsAheadError | None:
    """Block a hollow ship: the branch must have ≥1 commit ahead of base (#788).

    The phase gate answers "is the work attested?"; this answers "is
    there actually anything to ship?". Without it, a
    reviewer-approved-but-uncommitted (or committed-elsewhere) branch
    produced a hollow ``state: shipped`` — the CLI reported success, the
    FSM advanced, then ``execute_ship`` later failed with "No commits
    between main and branch", deferring the real failure into the async
    worker where it is easy to miss.

    Blocks **only on a confirmed zero** (``rev_count(base..branch) ==
    0``), naming the branch and the base it was compared against. If git
    introspection cannot be performed (no real repo, base undetectable,
    git error) the state is *unverifiable* — distinct from the
    confirmed-zero bug — so the prior behaviour (proceed) is preserved
    rather than blocking on an unknown.
    """
    repo = (worktree.extra or {}).get("worktree_path", "") or worktree.repo_path
    branch = worktree.branch
    if not repo or not branch:
        return None
    try:
        base = f"origin/{git.default_branch(repo=repo)}"
        ahead = git.rev_count(repo=repo, range_spec=f"{base}..{branch}")
    except (CommandFailedError, RuntimeError, ValueError):
        return None  # unverifiable ≠ the confirmed-zero bug — do not block
    if ahead > 0:
        return None
    return NoCommitsAheadError(
        error=(
            f"Refusing to ship: branch {branch!r} has 0 commits ahead of {base} — "
            f"nothing to push (work uncommitted or committed elsewhere). "
            f"Commit the work on {branch!r}, then retry `pr create`."
        ),
        branch=branch,
        base=base,
    )


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

    # #801 SSOT: canonical earliest selection (was -pk-latest); the
    # gate only READS — create=False so a gate check never mints a
    # session as a side effect.
    session = ticket.find_phase_session()
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
    # #776 N1: resolve the INVOKING worktree (same root cause as the
    # ship-branch fix) — a reused multi-workstream ticket must run visual
    # QA against the current workstream's repo, not the stale earliest
    # `worktrees.first()` row. The `ship_invoking_branch` hint is recorded
    # by `create()` before the gates run, so the canonical resolver has
    # the data here too.
    extra = cast("TicketExtra", ticket.extra or {})
    worktree = resolve_ship_worktree(ticket, extra)
    repo_path = worktree.repo_path if worktree else "."
    base_url = _resolve_base_url(worktree)

    overlay = get_overlay()
    diff = visual_qa.changed_files(repo=repo_path)
    report = visual_qa.evaluate(diff=diff, overlay=overlay, base_url=base_url, skip_reason=skip_reason)

    # Only persist when the gate produced a meaningful signal — skipping a
    # no-op run keeps the FSM history readable.
    if report.pages or report.has_errors:
        # #800 N3: canonical locked RMW — concurrent pr_urls (ship
        # worker) writer no longer clobbers visual_qa.
        ticket.merge_extra(set_keys={"visual_qa": report.summary()})

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
    return validate_pr_metadata(ticket, worktree)


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
        | NoCommitsAheadError
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
        # #779: refuse to read the shipping gate from a worktree-isolated DB.
        # The gate reads phase attestations; if this process resolved to a
        # per-worktree DB (uv run from a worktree) it sees a partial phase
        # set and blocks with a verbatim `missing: [...]` even though the
        # phases were recorded — just in the canonical DB. Fail loudly here
        # instead, naming the canonical DB and the correct command.
        assert_lifecycle_db_is_canonical(ticket)
        worktree = ticket.worktrees.first()  # ty: ignore[unresolved-attribute]
        if worktree is None:
            return WorktreeMissingError(error="ticket has no worktree")

        # #776: a ticket can span multiple PRs (one branch per
        # workstream). Record the INVOKING worktree's current git branch
        # so ShipExecutor ships THIS branch, not the earliest (often
        # already-merged) `worktrees.first()` row. Read from the cwd the
        # CLI was invoked in (the worktree the user ran `pr create` from).
        invoking_branch = git.current_branch(repo=".")
        if invoking_branch and invoking_branch not in {"HEAD", "main", "master"}:
            # #800 N3: canonical locked RMW (was a blind whole-extra
            # overwrite from a stale read — clobbered the ship worker's
            # pr_urls / visual_qa).
            ticket.merge_extra(set_keys={"ship_invoking_branch": invoking_branch})

        # #788: refuse a hollow ship — the branch ShipExecutor will
        # actually push (the #776/#800 canonical resolver, so the check
        # matches what is shipped) must have ≥1 commit ahead of base.
        # Placed before BOTH the gate and the --skip-validation
        # reconcile so no path can advance the FSM to a hollow SHIPPED.
        # `resolve_ship_worktree` cannot be None here — the
        # WorktreeMissingError check above guarantees a worktree (it
        # falls back to that same `worktrees.first()`).
        no_commits = _assert_commits_ahead_of_base(
            resolve_ship_worktree(ticket, cast("TicketExtra", ticket.extra or {})) or worktree
        )
        if no_commits is not None:
            return no_commits

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
            return ship_dry_run(ticket, worktree)
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

        return create_or_defer_pr(repo_path, branch_name)

    @command(name="check-gates")
    def check_gates(self, ticket_id: int, target_phase: str = "shipping") -> dict[str, object]:
        """Check whether session gates allow a phase transition."""
        from teatree.core.models.errors import QualityGateError  # noqa: PLC0415

        ticket = Ticket.objects.get(pk=ticket_id)
        # #801 SSOT: canonical earliest selection, read-only (no create).
        session = ticket.find_phase_session()
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
    def merge(self, pr: int, slug: str, *, repo_path: str = "", auto: bool = False) -> MergeResult:
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

        ``--repo-path`` pins all git ops to a specific clone of ``slug``
        (origin-asserted == slug). The review-loop sweeps the backlog
        from an arbitrary cwd, so it MUST pass the public souliane/* main
        clone path here for a deterministic target. When omitted, the
        process cwd is used.
        """
        from teatree.core.pr_merge import squash_merge_public  # noqa: PLC0415

        squash_merge_public(pr=pr, slug=slug, repo_path=repo_path, auto=auto)
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
