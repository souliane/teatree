"""Pull request helpers: gate-validate then enqueue ship transition.

The actual push + PR creation lives in ``ShipExecutor`` (BLUEPRINT §4) and runs
inside the ``execute_ship`` task. This command is a deterministic CLI wrapper:
it runs the deterministic gates, calls ``ticket.ship()`` to enter SHIPPED, and
returns the PR URL once the worker completes.
"""

import re
from typing import TYPE_CHECKING, TypedDict, cast

from django_typer.management import TyperCommand, command

from teatree.core.backend_factory import code_host_from_overlay
from teatree.core.db_anchor import assert_lifecycle_db_is_canonical
from teatree.core.gates.orphan_guard import BranchStatus, classify_branch
from teatree.core.management.commands._close_keyword_gate import run_close_keyword_gate
from teatree.core.management.commands._closes_issue_crosscheck import run_closes_issue_crosscheck
from teatree.core.management.commands._ensure_pr import EnsurePrResult, create_or_defer_pr
from teatree.core.management.commands._pr_preview import (
    PrValidationError,
    ShipDryRun,
    ship_dry_run,
    validate_pr_metadata,
)
from teatree.core.management.commands._pr_ticket_resolve import (
    TicketNotFoundError,
    resolve_ticket,
    ticket_not_found_error,
)
from teatree.core.management.commands._ship_exec import (
    ShipEnqueued,
    ShipExecuted,
    ShippingGateFailure,
    _enqueue_ship,
    _ship_sync,
)
from teatree.core.management.commands._ship_fsm import reconcile_fsm_for_ship
from teatree.core.management.commands._ship_gates import (
    BranchCurrencyFailure,
    E2EMandatoryGateFailure,
    NoCommitsAheadError,
    VisualQAGateFailure,
)
from teatree.core.management.commands._ship_gates import assert_commits_ahead_of_base as _assert_commits_ahead_of_base
from teatree.core.management.commands._ship_gates import check_shipping_gate as _check_shipping_gate
from teatree.core.management.commands._ship_gates import run_branch_currency_gate as _run_branch_currency_gate
from teatree.core.management.commands._ship_gates import run_e2e_mandatory_gate as _run_e2e_mandatory_gate
from teatree.core.management.commands._ship_gates import run_visual_qa_gate as _run_visual_qa_gate
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import Ticket, Worktree
from teatree.core.on_behalf_gate_recorded import (
    OnBehalfPostBlockedError,
    on_behalf_block_message,
    require_on_behalf_approval,
)
from teatree.core.on_behalf_post_receipt import notify_user_on_behalf_post
from teatree.core.overlay_loader import get_overlay
from teatree.core.public_identity import MergeResult
from teatree.core.runners.ship import resolve_and_reconcile_branch, resolve_ship_worktree
from teatree.types import RawAPIDict
from teatree.utils import git

if TYPE_CHECKING:
    from teatree.core.models.types import TicketExtra

# The host create/update-comment response shape returned by the comment commands.
type CommentResult = dict[str, object]

# VisualQAGateFailure / BranchCurrencyFailure / NoCommitsAheadError and the
# pre-ship gate helpers live in ``_ship_gates`` (extracted by concern,
# re-imported above under their legacy underscore names) so external importers
# of ``pr.VisualQAGateFailure`` / ``pr._run_visual_qa_gate`` keep working and
# ``pr.py`` stays within the module-health LOC bar. ``_run_ship_gates`` stays
# here so the ``patch.object(pr, "_run_visual_qa_gate")`` test seams resolve
# against this module's namespace.


# ShipDryRun / PrValidationError live in ``_pr_preview`` (re-exported below)
# so external importers of ``pr.ShipDryRun`` / ``pr.PrValidationError`` keep
# working after the ship-preview split.


# TicketNotFoundError lives in ``_pr_ticket_resolve`` (re-exported via the
# import above) so external importers of ``pr.TicketNotFoundError`` keep
# working after the ticket-resolution split.


class WorktreeMissingError(TypedDict):
    error: str


# ShipEnqueued / ShipExecuted / ShippingGateFailure and the ship-execution
# helpers live in ``_ship_exec`` (extracted by concern, re-exported above)
# so external importers of ``pr.ShipExecuted`` etc. keep working and
# ``pr.py`` stays within the module-health LOC bar.

# EnsurePrResult lives in ``_ensure_pr`` (re-exported above) so external
# importers of ``pr.EnsurePrResult`` keep working after the ensure-pr split.


_IMAGE_URL_RE = re.compile(r"!\[([^\]]*)\]\((/uploads/[^\)]+)\)")
_EXTERNAL_LINK_RE = re.compile(r"https?://(?:www\.)?(?:notion\.so|linear\.app|jira\.\S+)/\S+")


def _run_ship_gates(
    ticket: Ticket,
    worktree: Worktree,
    *,
    skip_visual_qa: str = "",
    title: str = "",
) -> (
    ShippingGateFailure
    | VisualQAGateFailure
    | BranchCurrencyFailure
    | E2EMandatoryGateFailure
    | PrValidationError
    | None
):
    """Run the pre-ship gates in order; return the first failure or ``None``.

    Composed out of ``create`` so the command stays within the
    return-count gate and the gate sequence is independently testable.
    The branch-currency gate (#940) runs FIRST: a stale base would
    otherwise poison the visual-QA gate (it would render the
    pre-merge tree) and the cold reviewer's SHA attestation.

    ``title`` is the explicit ``--title`` override: it has not yet been
    persisted to ``extra['pr_title_override']`` (that happens at ship time,
    after the gates pass), so it is threaded into ``validate_pr_metadata``
    here — otherwise the preflight would validate the regenerated commit
    subject rather than the title that will actually ship.
    """
    currency_error = _run_branch_currency_gate(ticket, worktree)
    if currency_error is not None:
        return currency_error
    gate_error = _check_shipping_gate(ticket)
    if gate_error is not None:
        return gate_error
    # Overlay-scoped (#1012): no-op unless the overlay forbids auto-close
    # trailers; raises SystemExit with the offending line otherwise.
    run_close_keyword_gate(ticket, worktree)
    # Overlay-scoped (#83): for overlays that auto-close via ``Closes #N``,
    # cross-check each referenced issue is real + open on the target repo —
    # a teatree task id is not an issue number. Raises SystemExit on a
    # closed/missing target; warns (non-blocking) on an unrelated title.
    run_closes_issue_crosscheck(ticket, worktree)
    visual_qa_error = _run_visual_qa_gate(ticket, skip_reason=skip_visual_qa)
    if visual_qa_error is not None:
        return visual_qa_error
    # #1967: a customer-display-impacting change needs green E2E evidence at the
    # reviewed tree (or a single-use user bypass). Runs after the diff-rendering
    # visual-QA gate so both see the post-branch-currency tree.
    e2e_error = _run_e2e_mandatory_gate(ticket)
    if e2e_error is not None:
        return e2e_error
    return validate_pr_metadata(ticket, worktree, title=title)


def _dispatch_ship(
    ticket: Ticket,
    worktree: Worktree,
    *,
    title: str,
    dry_run: bool,
    sync: bool,
) -> ShipDryRun | ShipExecuted | ShipEnqueued | ShippingGateFailure:
    """Pick the ship execution mode once the gates have all passed.

    ``dry_run`` previews without transitioning; ``sync`` runs the ship
    inline; the default enqueues it for a worker (#708).
    """
    if dry_run:
        return ship_dry_run(ticket, worktree, title=title)
    if sync:
        return _ship_sync(ticket, title)
    return _enqueue_ship(ticket, title)


class Command(TyperCommand):
    @command()
    # PLR0913: this signature IS the CLI contract — django-typer derives
    # --title/--dry-run/--skip-validation/--skip-visual-qa/--sync by
    # introspecting these kwargs; bundling them into a dataclass would delete
    # the flags. Same targeted-noqa-for-framework-reality pattern as the
    # repo's PLC0415 (import-in-function) and SLF001 (framework internals).
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def create(  # noqa: PLR0913
        self,
        ticket_id: str,
        *,
        title: str = "",
        dry_run: bool = False,
        skip_validation: bool = False,
        skip_mr_format_check: bool = False,
        skip_visual_qa: str = "",
        sync: bool = False,
    ) -> (
        ShipEnqueued
        | ShipExecuted
        | ShipDryRun
        | PrValidationError
        | VisualQAGateFailure
        | BranchCurrencyFailure
        | E2EMandatoryGateFailure
        | ShippingGateFailure
        | WorktreeMissingError
        | NoCommitsAheadError
        | TicketNotFoundError
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

        ``--skip-validation`` skips the heavy ship gates (visual QA, branch
        currency, FSM phase check) but STILL runs the cheap MR
        title/description format check. ``--skip-mr-format-check`` is the
        separate, explicit opt-in that disables that format check too — needed
        only in the rare case where a non-canonical title must ship anyway.
        """
        try:
            ticket = resolve_ticket(ticket_id)
        except Ticket.DoesNotExist:
            # #1051: no canonical Ticket row (out-of-FSM autonomous-loop
            # PR, or a pruned row). Return an actionable error instead of
            # letting the bare DoesNotExist crash the command and force a
            # manual `gh pr create` fallback.
            return ticket_not_found_error(ticket_id)
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

        # #1587: reconcile the recorded branch to the worktree's ACTUAL git
        # branch BEFORE any gate reads `worktree.branch` — the single chokepoint
        # shared with `ShipExecutor` so a renamed branch can no longer reach a
        # gate range query that fails fail-soft.
        ship_worktree = resolve_ship_worktree(ticket, cast("TicketExtra", ticket.extra or {})) or worktree
        repo_path = (ship_worktree.extra or {}).get("worktree_path", "") or ship_worktree.repo_path
        if repo_path:
            resolve_and_reconcile_branch(ticket, ship_worktree, repo_path)

        # #788: refuse a hollow ship — the branch ShipExecutor will
        # actually push (the #776/#800 canonical resolver, so the check
        # matches what is shipped) must have ≥1 commit ahead of base.
        # Placed before BOTH the gate and the --skip-validation
        # reconcile so no path can advance the FSM to a hollow SHIPPED.
        no_commits = _assert_commits_ahead_of_base(ship_worktree)
        if no_commits is not None:
            return no_commits

        if not skip_validation:
            gate_failure = _run_ship_gates(ticket, ship_worktree, skip_visual_qa=skip_visual_qa, title=title)
            if gate_failure is not None:
                return gate_failure
        else:
            # --skip-validation is the user-authorized attestation
            # substitute (the gate-fixer bootstrap, /t3:ship §5 #2). The
            # FSM must follow the authorization or ship() is structurally
            # impossible from a non-REVIEWED state (#748).
            reconcile_fsm_for_ship(ticket)
            # --skip-validation skips the HEAVY gates (visual QA, branch
            # currency, FSM phase check) — but NOT the cheap, deterministic
            # MR title/description format check. A non-compliant title must
            # not slip onto GitLab via the bypass; only the explicit
            # --skip-mr-format-check opt-in disables the format check too.
            if not skip_mr_format_check:
                format_error = validate_pr_metadata(ticket, ship_worktree, title=title)
                if format_error is not None:
                    return format_error

        return _dispatch_ship(ticket, ship_worktree, title=title, dry_run=dry_run, sync=sync)

    @command(name="ensure-pr")
    def ensure_pr(
        self,
        branch: str = "",
        repo: str = "",
    ) -> EnsurePrResult:
        """Create a PR for an orphan branch (idempotent, no-op when a PR already exists).

        An orphan is a branch with commits not on the repo's default branch
        (resolved per-repo via ``refs/remotes/origin/HEAD``) after subject-
        match + tree-equality checks and no open PR. When this runs inside a
        git pre-push hook for a *first* push, the branch is not yet on the
        remote — creating the PR is deferred so the push proceeds.
        """
        repo_path = repo or "."
        branch_name = branch or git.current_branch(repo=repo_path)
        if not branch_name or branch_name in {"HEAD", "main", "master"}:
            return EnsurePrResult(skipped="not on a feature branch", branch=branch_name)

        report = classify_branch(repo_path, branch_name)

        if report.status is BranchStatus.SYNCED:
            return EnsurePrResult(skipped="branch synced to default branch", branch=branch_name)
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
        """Check whether session gates allow a phase transition (#1118: cross-session)."""
        from teatree.core.models.errors import QualityGateError  # noqa: PLC0415

        canonical_target = normalize_phase(target_phase)
        ticket = Ticket.objects.get(pk=ticket_id)
        session = ticket.find_phase_session()
        if session is None:
            return {"allowed": False, "reason": "No active session", "missing": []}
        try:
            session.check_gate_across_ticket(canonical_target)
        except QualityGateError:
            visited, _ = ticket.aggregate_phase_records()
            canonical_visited = {normalize_phase(phase) for phase in visited}
            required = session._REQUIRED_PHASES.get(canonical_target, [])  # noqa: SLF001
            missing = [p for p in required if p not in canonical_visited]
            return {"allowed": False, "missing": missing, "reason": f"{target_phase} requires: {', '.join(missing)}"}
        except (ValueError, KeyError) as exc:
            return {"allowed": False, "reason": str(exc), "missing": []}
        else:
            return {"allowed": True, "target_phase": canonical_target}

    @command(name="merge")
    def merge(self, pr: int, slug: str, *, repo_path: str = "", auto: bool = False) -> MergeResult:
        """REMOVED — FSM-incoherent post-#863; refuses with a redirect to the §17.4 keystone.

        The old LOCAL-squash/server-side path bypassed ``MergeClear``
        validation, the ``expected_head_oid`` SHA-binding, the atomic
        CLEAR-consume + ``MergeAudit`` + attestation + ``mark_merged()``.
        It refuses symmetrically with the raw-merge prohibition guard so
        no out-of-band path survives (BLUEPRINT §17.1 invariant 8 / §17.4).
        Use ``ticket clear`` then ``ticket merge`` instead.
        """
        _ = (repo_path, auto)
        error = (
            f"`t3 <overlay> pr merge` is removed: FSM-incoherent post-#863 (no MergeClear "
            f"validation / SHA-binding / audit / mark_merged). Use the sanctioned keystone: "
            f"`t3 <overlay> ticket clear {pr} {slug} --reviewed-sha <sha> --reviewer-identity "
            f"<independent-reviewer> --blast-class <substrate|logic|docs>` then `t3 <overlay> "
            f"ticket merge <clear_id>` (substrate adds `--human-authorized <id>`). §17.1 inv 8 / §17.4."
        )
        return MergeResult(merged=False, pr=pr, slug=slug, auto=auto, error=error)

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

    @command(name="post-test-plan")
    def post_test_plan(
        self,
        mr_iid: int,
        repo: str = "",
        title: str = "Test Plan",
        body: str = "",
        files: list[str] | None = None,
    ) -> dict[str, object]:
        """Post a test plan as a PR comment. Uploads files and updates existing notes.

        Files (screenshots, videos) are uploaded and embedded as ``![name](url)`` in the body.
        If an existing note contains ``## Test Plan``, it is updated instead of creating a new one.

        Gated by ``on_behalf_post_mode`` (#960, BLOCK under ``ask`` /
        ``draft_or_ask``): the call is refused with no upload or host side
        effect when no recorded :class:`OnBehalfApproval` matches
        ``(<repo>!<mr>, "post_evidence")``. The ``"post_evidence"`` action key
        is PERSISTED on existing ``OnBehalfApproval`` rows, so it stays the wire
        value even though the command is now named ``post-test-plan``. The gate
        is inlined here (not at the ``code_host`` layer) so PR creation — which
        is not an on-behalf colleague-facing post — remains ungated.

        The legacy ``post-evidence`` name is kept as a hidden, deprecated alias
        for one release so existing scripts keep working.
        """
        host = code_host_from_overlay()
        if host is None:
            return {"error": "No code host configured (check overlay GitLab token)"}

        repo_path = repo or get_overlay().metadata.get_ci_project_path()
        target = f"{repo_path}!{mr_iid}"

        # Peek (non-consuming) so an unapproved post refuses before uploading
        # anything; the consume happens atomically with the comment post below.
        blocked = on_behalf_block_message(target, "post_evidence")
        if blocked:
            return {"error": blocked}

        # Upload files and build markdown embeds
        embeds: list[str] = []
        for filepath in files or []:
            upload = host.upload_file(repo=repo_path, filepath=filepath)
            md = upload.get("markdown", "")
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

        def _publish() -> RawAPIDict:
            if existing_note:
                comment_id = int(str(existing_note["id"]))
                self.stdout.write(f"  Updating existing note {comment_id}")
                return host.update_pr_comment(repo=repo_path, pr_iid=mr_iid, comment_id=comment_id, body=note_body)
            return host.post_pr_comment(repo=repo_path, pr_iid=mr_iid, body=note_body)

        try:
            # consume + post + audit atomic (#1879): a failed comment post
            # rolls back the consume and writes no audit.
            result = require_on_behalf_approval(target=target, action="post_evidence", publish=_publish)
        except OnBehalfPostBlockedError as blocked_now:
            return {"error": str(blocked_now)}

        notify_user_on_behalf_post(
            target=target,
            action="post_evidence",
            destination=target,
            artifact_url=str(result.get("web_url") or result.get("html_url") or target),
            summary=f"{title} on {target}",
        )
        return result

    @command(name="post-evidence", hidden=True, deprecated=True)
    def post_evidence(
        self,
        mr_iid: int,
        repo: str = "",
        title: str = "Test Plan",
        body: str = "",
        files: list[str] | None = None,
    ) -> CommentResult:
        """Deprecated alias for ``post-test-plan`` (renamed; kept one release for back-compat)."""
        return self.post_test_plan(mr_iid, repo=repo, title=title, body=body, files=files)
