"""Pull request helpers: gate-validate then enqueue ship transition.

The actual push + PR creation lives in ``ShipExecutor`` (BLUEPRINT §4) and runs
inside the ``execute_ship`` task. This command is a deterministic CLI wrapper:
it runs the deterministic gates, calls ``ticket.ship()`` to enter SHIPPED, and
returns the PR URL once the worker completes.
"""

import re
from typing import TYPE_CHECKING, cast

from django_typer.management import TyperCommand, command

from teatree.core.backend_factory import code_host_from_overlay
from teatree.core.evidence.test_plan_blocked_gate import BlockedTestPlanPostError
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
from teatree.core.management.commands._pr_worktree import WorktreeMissingError, _resolve_or_adopt_worktree
from teatree.core.management.commands._shared_code_host import no_code_host_error
from teatree.core.management.commands._ship.exec import (
    ShipEnqueued,
    ShipExecuted,
    ShippingGateFailure,
    _enqueue_ship,
    _ship_sync,
)
from teatree.core.management.commands._ship.fsm import reconcile_fsm_for_ship
from teatree.core.management.commands._ship.gates import (
    BranchCurrencyFailure,
    DebtDeltaGateFailure,
    E2EMandatoryGateFailure,
    FleetClaimFenceFailure,
    NoCommitsAheadError,
    PrBudgetGateFailure,
    VisualQAGateFailure,
)
from teatree.core.management.commands._ship.gates import assert_commits_ahead_of_base as _assert_commits_ahead_of_base
from teatree.core.management.commands._ship.gates import check_shipping_gate as _check_shipping_gate
from teatree.core.management.commands._ship.gates import run_branch_currency_gate as _run_branch_currency_gate
from teatree.core.management.commands._ship.gates import run_debt_delta_gate as _run_debt_delta_gate
from teatree.core.management.commands._ship.gates import run_e2e_mandatory_gate as _run_e2e_mandatory_gate
from teatree.core.management.commands._ship.gates import run_fleet_claim_fence_gate as _run_fleet_claim_fence_gate
from teatree.core.management.commands._ship.gates import run_pr_budget_gate as _run_pr_budget_gate
from teatree.core.management.commands._ship.gates import run_visual_qa_gate as _run_visual_qa_gate
from teatree.core.management.commands._test_plan.post import (
    MrTestPlanPost,
    TestPlanMediaError,
    post_mr_test_plan_comment,
)
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import Ticket, Worktree
from teatree.core.on_behalf_gate_recorded import OnBehalfPostBlockedError
from teatree.core.overlay_loader import get_overlay
from teatree.core.provision.db_anchor import assert_lifecycle_db_is_canonical
from teatree.core.provision.worktree_adopt import reopen_ticket_for_followup
from teatree.core.public_identity import MergeResult
from teatree.core.runners.ship import resolve_and_reconcile_branch, resolve_ship_worktree
from teatree.core.send_proxy import OutboundBlockedError
from teatree.types import RawAPIDict
from teatree.utils import git
from teatree.utils.run import CommandFailedError

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


# WorktreeMissingError / _worktree_missing_error / _resolve_or_adopt_worktree
# (the worktree-or-adopt resolver, #3327) live in ``_pr_worktree`` (re-exported
# above) so ``pr.py`` stays within the module-health LOC bar and external
# importers of ``pr.WorktreeMissingError`` keep working.


# ShipEnqueued / ShipExecuted / ShippingGateFailure and the ship-execution
# helpers live in ``_ship_exec`` (extracted by concern, re-exported above)
# so external importers of ``pr.ShipExecuted`` etc. keep working and
# ``pr.py`` stays within the module-health LOC bar.

# EnsurePrResult lives in ``_ensure_pr`` (re-exported above) so external
# importers of ``pr.EnsurePrResult`` keep working after the ensure-pr split.


_IMAGE_URL_RE = re.compile(r"!\[([^\]]*)\]\((/uploads/[^\)]+)\)")
_EXTERNAL_LINK_RE = re.compile(r"https?://(?:www\.)?(?:notion\.so|linear\.app|jira\.\S+)/\S+")


def _run_precheck_ship_gates(
    ticket: Ticket,
    worktree: Worktree,
) -> ShippingGateFailure | BranchCurrencyFailure | PrBudgetGateFailure | DebtDeltaGateFailure | None:
    """The cheap state/text prechecks — first failure or ``None``.

    Grouped so ``_run_ship_gates`` stays within the return-count gate and the
    cheap gates run as one fail-fast block before the expensive diff-rendering
    gates. Order: branch-currency (#940, first so the rest see the post-merge
    tree), the phase/shipping gate (#694), the PR-budget gate (north-star PR-2),
    then the debt-delta gate (north-star PR-3) — the last two both cheap, so a
    ticket over its open-PR budget or introducing net-new tech debt fails before
    any push creates an orphan remote branch.
    """
    currency_error = _run_branch_currency_gate(ticket, worktree)
    if currency_error is not None:
        return currency_error
    gate_error = _check_shipping_gate(ticket)
    if gate_error is not None:
        return gate_error
    budget_error = _run_pr_budget_gate(ticket, worktree)
    if budget_error is not None:
        return budget_error
    return _run_debt_delta_gate(ticket, worktree)


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
    | FleetClaimFenceFailure
    | PrBudgetGateFailure
    | DebtDeltaGateFailure
    | PrValidationError
    | None
):
    """Run the pre-ship gates in order; return the first failure or ``None``.

    Composed out of ``create`` so the command stays within the
    return-count gate and the gate sequence is independently testable.
    The cheap state/text prechecks (:func:`_run_precheck_ship_gates` —
    branch-currency, shipping, PR-budget, debt-delta) run FIRST as one
    fail-fast block, then the fleet-claim fence (fleet-safety Stage 2,
    inert unless the kill-switch is on), then the overlay close-keyword
    gates, then the expensive diff-rendering gates (visual-QA, mandatory-E2E)
    so all of them see the post-branch-currency tree.

    ``title`` is the explicit ``--title`` override: it has not yet been
    persisted to ``extra['pr_title_override']`` (that happens at ship time,
    after the gates pass), so it is threaded into ``validate_pr_metadata``
    here — otherwise the preflight would validate the regenerated commit
    subject rather than the title that will actually ship.
    """
    precheck_error = _run_precheck_ship_gates(ticket, worktree)
    if precheck_error is not None:
        return precheck_error
    # Fleet-safety Stage 2: refuse to open a PR for a claimed work item this
    # instance no longer holds (stolen claim, or ref infra unreachable). Inert
    # unless ``fleet_claim_enabled`` is on and the ticket carries a fleet-claim.
    fence_error = _run_fleet_claim_fence_gate(ticket, worktree)
    if fence_error is not None:
        return fence_error
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


def _run_skip_validation_path(
    ticket: Ticket,
    ship_worktree: Worktree,
    *,
    skip_mr_format_check: bool,
    title: str,
) -> PrValidationError | None:
    """Reconcile the FSM for a ``--skip-validation`` ship, then run the cheap format check.

    ``--skip-validation`` is the user-authorized attestation substitute (the
    gate-fixer bootstrap, /t3:ship §5 #2): the FSM must follow the authorization
    or ``ship()`` is structurally impossible from a non-REVIEWED state (#748). It
    skips the HEAVY gates (visual QA, branch currency, FSM phase check) but NOT
    the cheap, deterministic MR title/description format check — a non-compliant
    title must not slip onto GitLab via the bypass; only the explicit
    ``--skip-mr-format-check`` opt-in disables the format check too.
    """
    reconcile_fsm_for_ship(ticket)
    if skip_mr_format_check:
        return None
    return validate_pr_metadata(ticket, ship_worktree, title=title)


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


def _validate_repo_and_resolve_branch(repo: str, repo_path: str, branch: str) -> tuple[str, EnsurePrResult | None]:
    """Validate ``--repo`` is a real git checkout and resolve the branch to classify.

    ``--repo`` must be a filesystem path, never a forge slug (``owner/repo``) —
    ``git -C <slug>`` fails, and that failure used to be swallowed into a
    false SYNCED classification (#2937). Returns ``(branch_name, None)`` on
    success, or ``("", <result>)`` — the early :class:`EnsurePrResult` the
    caller returns as-is — when validation stops the command before
    classification.
    """
    if repo and not git.check(repo=repo_path, args=["rev-parse", "--is-inside-work-tree"]):
        return "", EnsurePrResult(
            error=(
                f"--repo {repo!r} is not a git checkout on this filesystem. Pass a "
                "path to a local clone or worktree (e.g. '.' or '/path/to/repo'), "
                "not a forge slug like 'owner/repo'."
            ),
        )
    branch_name = branch or git.current_branch(repo=repo_path)
    if not branch_name or branch_name in {"HEAD", "main", "master"}:
        return "", EnsurePrResult(skipped="not on a feature branch", branch=branch_name)
    return branch_name, None


class Command(TyperCommand):
    @command()
    # PLR0913: this signature IS the CLI contract — django-typer derives
    # --title/--dry-run/--skip-validation/--skip-visual-qa/--sync by
    # introspecting these kwargs; bundling them into a dataclass would delete
    # the flags. Same targeted-noqa-for-framework-reality pattern as the
    # repo's PLC0415 (import-in-function) and SLF001 (framework internals).
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def create(  # noqa: PLR0913 — wide signature by design: each parameter is a distinct required input
        self,
        ticket_id: str,
        *,
        title: str = "",
        dry_run: bool = False,
        skip_validation: bool = False,
        skip_mr_format_check: bool = False,
        skip_visual_qa: str = "",
        sync: bool = False,
        adopt_worktree: bool = False,
    ) -> (
        ShipEnqueued
        | ShipExecuted
        | ShipDryRun
        | PrValidationError
        | VisualQAGateFailure
        | BranchCurrencyFailure
        | E2EMandatoryGateFailure
        | FleetClaimFenceFailure
        | PrBudgetGateFailure
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

        ``--adopt-worktree`` opens a follow-up PR on a ticket whose prior PR
        already merged and whose worktree row was torn down (#3327): it attaches
        the invoking on-disk worktree as a new row and reopens the terminal
        ticket to a shippable state once the #788 hollow-ship guard confirms the
        fresh branch has real commits — so already-merged work is never re-shipped.
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
        # #3327: the follow-up-PR case — a terminal ticket whose prior PR's
        # worktree row was torn down. With --adopt-worktree, attach the invoking
        # on-disk worktree as a new row (guarded) and continue through the
        # managed path; without it (or on a never-provisioned ticket), refuse.
        resolved = _resolve_or_adopt_worktree(ticket, adopt_worktree=adopt_worktree)
        if not isinstance(resolved, Worktree):
            return resolved
        worktree = resolved

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

        # #3327: only after the #788 hollow-ship guard passes (the fresh branch
        # has real commits ahead of base) does an adopted terminal ticket get
        # reopened to a shippable FSM state — so a mistakenly-merged branch is
        # refused above, before any FSM advance, never re-shipped. A no-op unless
        # the ticket sits in MERGED/DELIVERED.
        if adopt_worktree:
            reopen_ticket_for_followup(ticket)

        if not skip_validation:
            gate_failure = _run_ship_gates(ticket, ship_worktree, skip_visual_qa=skip_visual_qa, title=title)
            if gate_failure is not None:
                return gate_failure
        else:
            format_error = _run_skip_validation_path(
                ticket, ship_worktree, skip_mr_format_check=skip_mr_format_check, title=title
            )
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

        ``--repo`` must be a filesystem path to a git checkout, never a forge
        slug (``owner/repo``) — validated up front so that mistake surfaces
        as a clear error instead of a silently misclassified branch (#2937).
        """
        repo_path = repo or "."
        branch_name, early_result = _validate_repo_and_resolve_branch(repo, repo_path, branch)
        if early_result is not None:
            return early_result

        try:
            report = classify_branch(repo_path, branch_name)
        except CommandFailedError as exc:
            return EnsurePrResult(
                branch=branch_name,
                error=f"could not determine sync status of {branch_name!r} in {repo_path!r}: {exc}",
            )

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
        from teatree.core.models.errors import QualityGateError  # noqa: PLC0415 — deferred: ORM/app-registry

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
            required = session._REQUIRED_PHASES.get(canonical_target, [])  # noqa: SLF001 — intentional access to a sibling's internal within the same subsystem
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
            return no_code_host_error()
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
            return no_code_host_error()

        author = get_overlay().config.get_gitlab_username() or host.current_user()
        if not author:
            return {
                "error": "Could not resolve author username — "
                "set it with `t3 <overlay> config_setting set <host>_username <value>`",
            }

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

        A thin delegator to the shared gated engine
        (:func:`teatree.core.management.commands._test_plan.post.post_mr_test_plan_comment`),
        so the MR path gets the SAME gates as the ticket/issue poster (F3.1) and
        can no longer drift: files (screenshots, videos) are uploaded and each one
        passes the #2156 ``verify_upload`` existence check before it is embedded
        as ``![name](url)``; the body is run through the blocked-body config gate
        and the scanned public-repo leak seam; and the note is matched for an
        idempotent in-place update by THIS MR's hidden idempotency marker — never
        a naive ``"## Test Plan" in body`` scan that could clobber a colleague's
        unrelated comment.

        Gated by ``on_behalf_post_mode`` (#960, BLOCK under ``ask`` /
        ``draft_or_ask``): the call is refused with no upload or host side
        effect when no recorded :class:`OnBehalfApproval` matches
        ``(<repo>!<mr>, "post_evidence")``. The ``"post_evidence"`` action key
        is PERSISTED on existing ``OnBehalfApproval`` rows, so it stays the wire
        value even though the command is now named ``post-test-plan``. The gate
        is inlined at the command layer (not at the ``code_host`` layer) so PR
        creation — which is not an on-behalf colleague-facing post — remains
        ungated.

        The legacy ``post-evidence`` name is kept as a hidden, deprecated alias
        for one release so existing scripts keep working.
        """
        host = code_host_from_overlay()
        if host is None:
            return no_code_host_error()

        repo_path = repo or get_overlay().metadata.get_ci_project_path()
        try:
            # The MR path is a thin delegator to the shared gated engine (F3.1):
            # the on-behalf peek, the #2156 verify_upload existence check, the
            # blocked-body config gate, the scanned forge-write seam, and the
            # marker-scoped note match are the SAME as the ticket/issue poster, so
            # the two paths can never drift.
            return post_mr_test_plan_comment(
                host,
                MrTestPlanPost(repo=repo_path, mr_iid=mr_iid, title=title, body=body, files=list(files or [])),
                write_out=self.stdout.write,
            )
        except (OnBehalfPostBlockedError, OutboundBlockedError, BlockedTestPlanPostError, TestPlanMediaError) as err:
            return {"error": str(err)}

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
