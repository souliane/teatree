"""The missing ``t3`` merge FSM transition — loop-executes side (BLUEPRINT §17.4).

This is the keystone the factory was missing: the only sanctioned path from
``IN_REVIEW`` → ``MERGED``. Raw ``gh pr merge`` / ``glab mr merge`` bypasses the
ledger update, the HEAD/workstream attestation binding, the privacy/AI-signature
scan, and ``mark_merged()`` — leaving the FSM incoherent. The prohibition guard
(``hook_router._BLOCKED_COMMANDS``) mechanically refuses the raw path; this
module is the coherent replacement.

Flow (orchestrator-decides / loop-executes, §17.4.1):

Pre-condition hook — ``assert_merge_preconditions`` runs the loop's §17.4.3
validation in order: a valid, actionable ``MergeClear`` row re-read from the
DB; CI green on the exact PR head; an independent cold-review CLEAR recorded
(a ``reviewer_identity`` distinct from the executing loop — §17.8 clause 3);
plus the §17.4.3 SHA-match and not-draft checks. ``substrate`` blast-class PRs
are never auto-merged here (invariant 4 / §17.4.3 step 5).

Atomic merge — ``execute_bound_merge`` binds the merge to
``expected_head_oid`` so a force-push landing in the TOCTOU window is rejected
by GitHub and treated as a failed check, never a retry-with-new-head (the
E10-class staleness/replay defence).

Post hook — ``record_merge_and_advance`` runs in one ``transaction.atomic()``:
consume the CLEAR, write the ``MergeAudit`` row, bind the phase attestation to
the merged HEAD, and call ``ticket.mark_merged()``. State-change and the
durable merge record land atomically (the §4 worker-enqueue / sync-atomicity
invariant).

Lost-post-hook recovery (#928) — the irreversible GitHub merge necessarily
runs *before* the post hook can consume the single-use CLEAR. If the process
dies between the two (kill / DB lock / rollback), the PR is merged on GitHub
but the CLEAR is unconsumed and the FSM never advanced; re-issuing the merge
would fail forever (GitHub 405s an already-merged PR). The retry therefore
*reconciles*: when GitHub reports the PR already MERGED at the exact
``reviewed_sha`` tree (the head still bound to the reviewed commit), the
irreversible merge is skipped and only the idempotent post hook runs — the
same single-use CLEAR is consumed exactly once under the row lock. A lost
post hook is recoverable, never a permanent "merged-on-GitHub, not-in-FSM"
brick. This does not weaken the single-use, SHA-bind, or maker≠checker
guarantees: the authorization guards run *before* reconciliation, and the
row-locked single-use re-check is unchanged.
"""

import json
import logging
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict, cast
from urllib.parse import urlparse

from django.apps import apps
from django.db import transaction
from django.utils import timezone

from teatree.project import find_project_root
from teatree.utils import git
from teatree.utils.run import run_allowed_to_fail
from teatree.utils.url_slug import slug_from_issue_or_pr_url

if TYPE_CHECKING:
    from teatree.core.models import MergeClear

logger = logging.getLogger(__name__)


class MergePreconditionError(RuntimeError):
    """A §17.4.3 pre-condition check failed — the loop must not merge.

    The caller re-escalates into the durable backlog (it never self-issues a
    replacement CLEAR) and leaves the FSM unchanged.
    """


class MergeHeadMovedError(MergePreconditionError):
    """GitHub rejected the merge because the head moved off ``expected_head_oid``.

    Treated as a failed check, NOT a retry-with-new-head (§17.4.3): the loop
    never re-resolves the head and proceeds.
    """


class MergeReplayError(MergePreconditionError):
    """The CLEAR was already consumed when re-checked UNDER the row lock.

    ``assert_merge_preconditions`` reads ``is_actionable()`` without the row
    lock; two executors that both pass that unlocked check would otherwise
    both reach the post hook and double-consume the single-use CLEAR (a
    double ``MergeAudit`` / double ``mark_merged()``). The post hook re-reads
    the row ``select_for_update``-locked and re-asserts ``consumed_at is
    None`` so exactly one executor wins; the loser raises this.
    """


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    pr_id: int
    slug: str
    merged_sha: str
    ticket_state: str


@dataclass(frozen=True, slots=True)
class MergePrecheck:
    """Outcome of :func:`assert_merge_preconditions`.

    ``verified_sha`` is the SHA the merge binds to (``expected_head_oid``).
    ``already_merged_sha`` is non-empty only when the §928 reconciliation
    fired: GitHub reports the PR already MERGED at the exact reviewed tree
    (a lost post-hook), so the irreversible merge must be SKIPPED and the
    post hook run idempotently against the existing merge commit.
    """

    verified_sha: str
    already_merged_sha: str = ""

    @property
    def needs_reconcile(self) -> bool:
        return bool(self.already_merged_sha)


def _run_gh(argv: list[str]) -> tuple[int, str, str]:
    gh = shutil.which("gh") or "gh"
    result = run_allowed_to_fail([gh, *argv], expected_codes=None)
    return result.returncode, result.stdout, result.stderr


_GIT_BRANCH_PREFIXES = frozenset(
    {
        "fix",
        "feat",
        "feature",
        "chore",
        "docs",
        "bugfix",
        "hotfix",
        "release",
        "refactor",
        "test",
        "ci",
        "build",
        "perf",
        "style",
        # Personal-workflow prefixes the user's branches actually carry.
        # ``ac/`` is the user's initials; ``wip/``, ``dev/``, and ``tmp/``
        # are common scratch / iteration namespaces. They are NOT GitHub
        # owners, so any CLEAR slug whose first segment matches must fall
        # through to the ticket-issue-url / clone-origin fallbacks (#1005).
        # Keep these — they're load-bearing for the user's day-to-day
        # merges, not "non-standard" prefixes to strip.
        "ac",
        "wip",
        "dev",
        "tmp",
    }
)


def _looks_like_owner_repo(slug: str) -> bool:
    """True when *slug* is already a GitHub ``owner/repo`` identifier.

    A workstream slug (``statusline-stale-wakeup``) has no ``/``; a repo
    slug (``souliane/teatree``) has exactly one path separator and is not
    a filesystem path.

    A *branch-shaped* slug (``fix/review-cli-django-bootstrap``,
    ``ac/cli-bundle-…``) also has exactly one ``/`` and would otherwise
    pass the structural check — yet it is a git branch name, not an
    ``owner/repo`` (#1005). Such a slug must fall through to the
    ticket-issue-url and clone-origin fallbacks so the real repo is
    resolved. A real GitHub owner cannot be one of the standard git
    branch namespaces (``fix``, ``feat``, ``chore``, …) nor the user's
    personal-workflow prefixes (``ac``, ``wip``, ``dev``, ``tmp``), so
    any slug whose first path segment is in
    :data:`_GIT_BRANCH_PREFIXES` (case-insensitive) is rejected here.
    The alternative — re-ordering :func:`resolve_pr_repo_slug` to consult
    the ticket/clone fallbacks before the structural check — would change
    the documented resolution order and weaken back-compat with rows that
    deliberately store an ``owner/repo`` slug; tightening this predicate
    is a smaller-surface fix.
    """
    if not ("/" in slug and not slug.startswith("/") and ":" not in slug and slug.count("/") >= 1):
        return False
    first_segment = slug.split("/", 1)[0]
    return first_segment.lower() not in _GIT_BRANCH_PREFIXES


def _project_repo_slug() -> str:
    """The GitHub ``owner/repo`` for the running teatree clone, or ``""``.

    Resolved from the project root's ``origin`` git remote — the same
    canonical :func:`git.remote_slug` path ``_ensure_pr.py`` /
    ``backends.github`` use to target ``gh`` at the right repo.
    """
    root = find_project_root()
    if root is None:
        return ""
    return git.remote_slug(repo=str(root))


def _ticket_repo_slug(clear: object) -> str:
    """The GitHub ``owner/repo`` for *clear*'s ticket, or ``""`` (#931).

    Resolved from the CLEAR's ``ticket.issue_url`` via the canonical
    :func:`slug_from_issue_or_pr_url` parser — the repo the PR genuinely
    belongs to, independent of which clone is running. This is the
    authoritative source when an overlay's GitHub repo differs from the
    editable ``t3`` clone's ``origin``: such a CLEAR must bind its
    live-head check to the overlay repo's PR, never to a same-numbered
    PR in the clone-origin repo (#931).
    """
    ticket = getattr(clear, "ticket", None)
    if ticket is None:
        return ""
    issue_url = str(getattr(ticket, "issue_url", "") or "")
    if not issue_url:
        return ""
    return slug_from_issue_or_pr_url(urlparse(issue_url).path)


def resolve_pr_repo_slug(clear: object) -> str:
    """The GitHub ``owner/repo`` to target ``gh`` at for *clear*'s PR.

    ``MergeClear.slug`` is a *workstream* slug, not a repo. Resolution
    order, first non-empty wins:

    (1) an ``owner/repo``-shaped slug is used as-is (back-compat with
    rows / tests that stored a repo there).
    (2) the CLEAR's ``ticket.issue_url`` repo (#931 — authoritative: the
    repo the PR belongs to, correct even when the overlay's repo differs
    from the running clone's ``origin``).
    (3) the running clone's ``origin`` git remote (the teatree-self
    overlay, whose repo *is* the clone origin).

    Fails closed with an actionable :class:`MergePreconditionError` when
    none yields a repo — never the opaque "could not resolve the live
    head" escalation that hid this gap.
    """
    slug = str(getattr(clear, "slug", "") or "")
    pr_id = getattr(clear, "pr_id", "?")
    if _looks_like_owner_repo(slug):
        return slug
    from_ticket = _ticket_repo_slug(clear)
    if from_ticket:
        return from_ticket
    resolved = _project_repo_slug()
    if resolved:
        return resolved
    msg = (
        f"could not resolve the GitHub repo for {slug}#{pr_id}: the CLEAR slug "
        f"{slug!r} is a workstream slug (not owner/repo), the CLEAR's ticket has "
        f"no recognisable GitHub issue_url, and the running teatree clone has no "
        f"resolvable 'origin' remote. The sanctioned merge needs the real repo to "
        f"bind the merge — re-issue the CLEAR from a checkout whose 'origin' points "
        f"at the GitHub repo, or pass an owner/repo slug."
    )
    raise MergePreconditionError(msg)


def fetch_live_head_sha(slug: str, pr_id: int) -> str:
    """The PR's current head SHA from GitHub (never a branch ref) — §17.4.3 step 2."""
    rc, out, _ = _run_gh(
        ["pr", "view", str(pr_id), "--repo", slug, "--json", "headRefOid", "--jq", ".headRefOid"],
    )
    return out.strip() if rc == 0 else ""


@dataclass(frozen=True, slots=True)
class PrMergeState:
    """The PR's merge state from GitHub — used for the §928 reconciliation.

    ``state`` is GitHub's PR state (``OPEN`` / ``MERGED`` / ``CLOSED``);
    ``merge_commit_oid`` is the resulting squash/merge commit when the PR
    is already merged (else ``""``).
    """

    state: str
    merge_commit_oid: str

    @property
    def is_merged(self) -> bool:
        return self.state.upper() == "MERGED"


def fetch_pr_merge_state(slug: str, pr_id: int) -> PrMergeState:
    """Whether the PR is already merged, and at which commit — §928 reconciliation.

    A lost post-hook (process kill / DB lock / rollback between
    :func:`execute_bound_merge` and :func:`record_merge_and_advance`)
    leaves the PR merged on GitHub while the CLEAR is still unconsumed
    and the FSM has not advanced. The retry must detect "already merged
    by us" and run the post hook idempotently rather than re-issuing the
    irreversible merge (which GitHub refuses with 405 — a permanent
    brick) or failing the SHA precondition forever. Returns an empty
    state on any ``gh`` error so the caller falls through to the normal
    (fail-closed) precondition path.
    """
    rc, out, _ = _run_gh(
        ["pr", "view", str(pr_id), "--repo", slug, "--json", "state,mergeCommit"],
    )
    if rc != 0 or not out.strip():
        return PrMergeState(state="", merge_commit_oid="")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return PrMergeState(state="", merge_commit_oid="")
    if not isinstance(data, dict):
        return PrMergeState(state="", merge_commit_oid="")
    state = str(data.get("state") or "")
    merge_commit = data.get("mergeCommit")
    oid = str(merge_commit.get("oid") or "") if isinstance(merge_commit, dict) else ""
    return PrMergeState(state=state, merge_commit_oid=oid)


def fetch_pr_is_draft(slug: str, pr_id: int) -> bool:
    """Whether the PR is in draft state — §17.4.3 step 4."""
    rc, out, _ = _run_gh(
        ["pr", "view", str(pr_id), "--repo", slug, "--json", "isDraft", "--jq", ".isDraft"],
    )
    return rc == 0 and out.strip().lower() == "true"


class _RollupEntry(TypedDict, total=False):
    """One ``gh ... statusCheckRollup`` entry — CheckRun or StatusContext."""

    conclusion: object
    status: object
    state: object


def _classify_check(check: object) -> str:
    """Map one rollup entry to ``green`` / ``pending`` / ``failed``.

    CheckRun entries use ``conclusion`` + ``status``; legacy StatusContext
    entries use ``state``. A non-dict entry is ignored by the caller.
    """
    if not isinstance(check, dict):
        return ""
    entry = cast("_RollupEntry", check)
    conclusion = str(entry.get("conclusion") or "").upper()
    status = str(entry.get("status") or "").upper()
    state = str(entry.get("state") or "").upper()
    if status and status != "COMPLETED":
        return "pending"
    if conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"} or state == "SUCCESS":
        return "green"
    if state == "PENDING":
        return "pending"
    return "failed"


def _rollup_verdict(statuses: list[str]) -> str:
    if "failed" in statuses:
        return "failed"
    if "pending" in statuses:
        return "pending"
    return "green"


def fetch_required_checks_status(slug: str, pr_id: int) -> str:
    """Live required-checks rollup for the PR head — §17.4.3 step 3.

    Evaluated against GitHub's live rollup at merge time (the authoritative
    set), NOT the ``gh_verify_result`` snapshot saved on the CLEAR. Returns
    ``"green"`` only when every reported check concluded successfully;
    ``"pending"`` while any is still running; otherwise the failing state.
    """
    rc, out, _ = _run_gh(
        [
            "pr",
            "view",
            str(pr_id),
            "--repo",
            slug,
            "--json",
            "statusCheckRollup",
            "--jq",
            ".statusCheckRollup",
        ],
    )
    if rc != 0:
        return "failed"
    try:
        rollup = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        return "failed"
    if not isinstance(rollup, list):
        return "failed"
    statuses = [verdict for check in rollup if (verdict := _classify_check(check))]
    return _rollup_verdict(statuses) if statuses else "green"


def _assert_clear_authorized(
    *,
    clear: object,
    executing_loop_identity: str,
    slug: str,
    pr_id: int,
    human_authorized: str,
) -> "MergeClear":
    """The §17.4.3 identity/substrate authorization guards (steps 1 + 5).

    Split out of :func:`assert_merge_preconditions` so the orchestration
    there reads as the ordered §17.4.3 sequence (authorize → SHA →
    reconcile → draft → checks) rather than one deeply-branching block.
    Raises :class:`MergePreconditionError` on the first failed guard;
    returns the narrowed :class:`MergeClear` on success.
    """
    from teatree.core.models import MergeClear  # noqa: PLC0415

    if not isinstance(clear, MergeClear):
        msg = f"no MergeClear row for {slug}#{pr_id} — refusing to merge (§17.4.3 step 1)"
        raise MergePreconditionError(msg)

    # 1. CLEAR exists, all fields populated, unconsumed.
    if not clear.is_actionable():
        msg = (
            f"MergeClear for {slug}#{pr_id} is not actionable (missing fields or already "
            f"consumed) — treated as absent (§17.4.2/§17.4.3 step 1)"
        )
        raise MergePreconditionError(msg)

    # Independent cold-review CLEAR: the reviewer identity must be distinct
    # from the executing loop (§17.8 clause 3 — the loop cannot rubber-stamp
    # its own CLEAR).
    if clear.reviewer_identity.strip() == executing_loop_identity.strip():
        msg = (
            f"MergeClear reviewer_identity ({clear.reviewer_identity!r}) equals the "
            f"executing loop identity — a CLEAR must be issued by an independent "
            f"cold reviewer, not self-issued (§17.8 clause 3)"
        )
        raise MergePreconditionError(msg)

    # The human-substrate escape is substrate-only. Presenting it against a
    # non-substrate CLEAR is refused outright so the path can never be used to
    # short-circuit independent loop review of a logic/docs PR (the loop is
    # the reviewer-of-record for those — invariant 8 / §17.4.1).
    presented = human_authorized.strip()
    if presented and not clear.is_substrate():
        msg = (
            f"--human-authorized presented for non-substrate MergeClear "
            f"({slug}#{pr_id}, blast_class={clear.blast_class}); the recorded-human-"
            f"approval path is substrate-only — a logic/docs CLEAR merges through "
            f"the loop, not via a human-approval escape hatch (invariant 8 / §17.4.1)"
        )
        raise MergePreconditionError(msg)

    # 5. blast_class respected — the loop NEVER auto-merges substrate-class
    #    PRs regardless of CLEAR validity (invariant 4 / §17.4.3 step 5). The
    #    ONLY exception: a substrate CLEAR whose recorded ``human_authorizer``
    #    matches the value re-presented at merge time. The recorded human
    #    approval is the gate; the AGENT then executes through this same
    #    SHA-bound, audited transition (invariant 8) — not raw ``gh``, not a
    #    human-performed merge. The approval is recorded durably on the CLEAR
    #    and bound to the merge.
    if clear.is_substrate() and not clear.human_merge_authorized_by(presented):
        detail = (
            "no human authoriser recorded on the CLEAR"
            if not clear.human_authorizer
            else f"presented authoriser != recorded ({clear.human_authorizer!r})"
            if presented
            else "no --human-authorized presented at merge time"
        )
        msg = (
            f"MergeClear for {slug}#{pr_id} is blast_class=substrate — substrate "
            f"changes require a recorded human approval and are draft-locked "
            f"(invariant 4); the loop never auto-merges them (§17.4.3 step 5). "
            f"{detail.capitalize()}. The sanctioned path: an owner issues `t3 "
            f"<overlay> ticket clear … --blast-class substrate --human-authorize "
            f"<id>` (the recorded approval — the gate), then the agent executes "
            f"`t3 <overlay> ticket merge <clear_id> --human-authorized <id>`"
        )
        raise MergePreconditionError(msg)

    return clear


def _reconcile_if_already_merged(*, slug: str, pr_id: int, live_sha: str) -> "MergePrecheck | None":
    """§928 reconciliation — the recovery path for a lost post-merge hook.

    Called only after the SHA re-check has passed (the head still equals
    ``reviewed_sha`` — a squash merge does not move the source-branch
    tip). If GitHub also reports the PR already MERGED, a prior attempt's
    irreversible merge LANDED but its post hook was lost (process kill /
    DB lock / rollback between :func:`execute_bound_merge` and
    :func:`record_merge_and_advance`). Re-issuing the merge would 405
    forever and the SHA gate can never self-heal — a permanent
    "merged-on-GitHub, not-in-FSM" brick. Because the head is still bound
    to the exact reviewed tree AND every guard in
    :func:`assert_merge_preconditions` (actionable / reviewer≠loop /
    substrate refusal) has already passed, completing the post hook
    idempotently against the existing merge commit is sound and weakens
    no guarantee. Returns ``None`` when the PR is not (yet) merged so the
    caller proceeds with the normal fresh-merge path.
    """
    merge_state = fetch_pr_merge_state(slug, pr_id)
    if not merge_state.is_merged:
        return None
    return MergePrecheck(
        verified_sha=live_sha,
        already_merged_sha=merge_state.merge_commit_oid or live_sha,
    )


def assert_merge_preconditions(
    *,
    clear: object,
    executing_loop_identity: str,
    slug: str,
    pr_id: int,
    human_authorized: str = "",
) -> MergePrecheck:
    """Run the §17.4.3 loop validation in order; return the :class:`MergePrecheck`.

    Raises :class:`MergePreconditionError` on the first failed check. The
    durable-backlog re-escalation is the caller's responsibility (§17.4.3) —
    this function never self-issues a replacement CLEAR.

    §928 reconciliation: the substrate / reviewer-identity / actionable
    guards run FIRST (so a stale CLEAR can never be reconciled past
    maker≠checker or the substrate auto-merge refusal). Only then, if
    GitHub reports the PR already MERGED at the exact ``reviewed_sha``
    tree, the returned precheck signals ``needs_reconcile`` so the caller
    runs the post hook idempotently instead of re-issuing the merge — a
    lost post-hook becomes recoverable rather than a permanent brick.

    ``human_authorized`` is the only escape from the substrate auto-merge
    refusal (step 5). It is empty for every loop-driven merge, so the loop
    still never auto-merges substrate. A non-empty value unlocks the merge
    **only** when the CLEAR is substrate-class AND its recorded
    ``human_authorizer`` matches: the substrate change requires a recorded
    human authorisation, and on re-presentation **the agent executes** the
    merge through this same sanctioned ``t3`` transition (invariant 8: even an
    owner-approved merge goes through this transition, never raw ``gh`` and
    never a human-performed merge action — approval is the gate, execution is
    always the agent). It can never unlock a non-substrate CLEAR, so it cannot
    be used to bypass independent loop review of logic/docs PRs.
    """
    authorized_clear = _assert_clear_authorized(
        clear=clear,
        executing_loop_identity=executing_loop_identity,
        slug=slug,
        pr_id=pr_id,
        human_authorized=human_authorized,
    )

    # 2. SHA still matches — re-fetch the live head; it must equal reviewed_sha.
    live_sha = fetch_live_head_sha(slug, pr_id)
    if not live_sha:
        msg = f"could not resolve the live head SHA for {slug}#{pr_id} (§17.4.3 step 2)"
        raise MergePreconditionError(msg)
    if live_sha != authorized_clear.reviewed_sha:
        # Show full SHAs (not [:8] prefixes) so a length-mismatch or any other
        # silent difference is obvious in the diagnostic (#1162).
        reviewed_sha = authorized_clear.reviewed_sha
        msg = (
            f"PR head moved: live={live_sha} (length={len(live_sha)}) != "
            f"reviewed={reviewed_sha} (length={len(reviewed_sha)}) — "
            f"the CLEAR is stale (force-push / new commits) or was issued with a "
            f"truncated SHA. Re-escalate; the loop never self-issues a replacement "
            f"(§17.4.3 step 2)"
        )
        raise MergePreconditionError(msg)

    reconcile = _reconcile_if_already_merged(slug=slug, pr_id=pr_id, live_sha=live_sha)
    if reconcile is not None:
        return reconcile

    # 4. Not draft.
    if fetch_pr_is_draft(slug, pr_id):
        msg = f"{slug}#{pr_id} is in draft state — refusing to merge (§17.4.3 step 4)"
        raise MergePreconditionError(msg)

    # 3. CI still green — against GitHub's LIVE rollup, not the saved snapshot.
    checks = fetch_required_checks_status(slug, pr_id)
    if checks != "green":
        msg = (
            f"live required-checks for {slug}#{pr_id} are {checks!r}, not green — "
            f"refusing to merge (§17.4.3 step 3; the live list is the source of "
            f"truth, not the CLEAR snapshot)"
        )
        raise MergePreconditionError(msg)

    return MergePrecheck(verified_sha=live_sha)


def execute_bound_merge(*, slug: str, pr_id: int, expected_head_oid: str) -> str:
    """Squash-merge bound to ``expected_head_oid`` — fail closed on head drift.

    Uses the GitHub merge API ``expected_head_oid`` parameter (``PUT
    .../pulls/N/merge``). If GitHub reports the head moved, the merge is
    refused and raised as :class:`MergeHeadMovedError` — a failed check, never
    a retry-with-new-head (§17.4.3 "bind execution to the exact verified SHA,
    fail closed").
    """
    endpoint = f"repos/{slug}/pulls/{pr_id}/merge"
    rc, out, err = _run_gh(
        [
            "api",
            "--method",
            "PUT",
            endpoint,
            "-f",
            "merge_method=squash",
            "-f",
            f"sha={expected_head_oid}",
        ],
    )
    if rc != 0:
        combined = f"{out}\n{err}".lower()
        if "head" in combined and ("modif" in combined or "changed" in combined or "409" in combined):
            msg = (
                f"GitHub refused the merge of {slug}#{pr_id}: head moved off "
                f"{expected_head_oid[:8]} (expected_head_oid mismatch). Treated as a "
                f"failed check — NOT retried with a new head (§17.4.3)"
            )
            raise MergeHeadMovedError(msg)
        msg = f"merge of {slug}#{pr_id} failed: {err.strip() or out.strip() or 'gh api non-zero'}"
        raise MergePreconditionError(msg)

    try:
        merged = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        merged = {}
    merged_sha = str(merged.get("sha") or "") if isinstance(merged, dict) else ""
    return merged_sha or expected_head_oid


def record_merge_and_advance(
    *,
    clear: object,
    merged_sha: str,
    required_checks_status: str,
) -> str:
    """Post hook: consume CLEAR, write audit, bind attestation, ``mark_merged()``.

    All in ONE ``transaction.atomic()`` so the FSM advance and the durable
    merge record land atomically (the §4 worker-enqueue / sync-atomicity
    invariant): a crash *within* this post hook rolls back the whole
    transaction, leaving the CLEAR unconsumed and the FSM unmoved — a
    re-runnable state. A crash *between* the irreversible GitHub merge and
    this hook also leaves the CLEAR unconsumed, but the PR is now merged on
    GitHub; that case is recovered by the #928 reconciliation in
    :func:`assert_merge_preconditions` (the retry detects "already merged
    at ``reviewed_sha``" and runs this hook idempotently instead of
    re-issuing the merge). Returns the resulting ticket state.
    """
    from teatree.core.models import MergeClear  # noqa: PLC0415

    if not isinstance(clear, MergeClear):  # pragma: no cover - guarded by caller
        msg = "record_merge_and_advance requires a MergeClear instance"
        raise MergePreconditionError(msg)

    merge_audit_model = apps.get_model("core", "MergeAudit")
    with transaction.atomic():
        locked = MergeClear.objects.select_for_update().get(pk=clear.pk)
        # Re-assert single-use UNDER the row lock. ``assert_merge_preconditions``
        # checked ``is_actionable()`` unlocked; two concurrent executors that
        # both passed it must not both consume — exactly one wins this
        # serialized re-check, the loser raises ``MergeReplayError`` and
        # writes no audit / does not advance the FSM.
        if locked.consumed_at is not None:
            msg = (
                f"MergeClear {locked.pk} ({locked.slug}#{locked.pr_id}) was already "
                f"consumed at {locked.consumed_at.isoformat()} — concurrent double-merge "
                f"refused under the row lock (§17.4.3 single-use replay defence)"
            )
            raise MergeReplayError(msg)
        locked.consumed_at = timezone.now()
        locked.save(update_fields=["consumed_at"])
        merge_audit_model.objects.create(
            clear=locked,
            merged_sha=merged_sha,
            required_checks_status=required_checks_status,
        )
        ticket = locked.ticket
        if ticket is None:
            return ""
        # Bind the phase attestation to the merged HEAD/workstream it was
        # earned against (the §17.6 enforcement candidate (7), absorbed
        # here): the canonical phase session records the SHA that actually
        # landed, so a later stale-workstream attestation cannot be reused
        # against a different HEAD.
        session = ticket.resolve_phase_session(agent_id="merge-loop")
        session.visit_phase("merged", agent_id=f"merge-loop@{merged_sha[:12]}")
        if ticket.state in {"in_review", "merged"}:
            ticket.mark_merged()
            ticket.save()
        return ticket.state


def merge_ticket_pr(
    *,
    clear: object,
    executing_loop_identity: str,
    human_authorized: str = "",
) -> MergeOutcome:
    """The full keystone transition: pre-condition → atomic merge → post hook.

    This is what the ``t3 <overlay> ticket merge`` CLI / durable loop calls.
    Any :class:`MergePreconditionError` propagates unchanged so the caller can
    write the durable-backlog re-escalation (§17.4.3) and leave the FSM
    untouched — the transition is all-or-nothing.

    ``human_authorized`` is empty for every loop-driven merge (the loop never
    auto-merges substrate). For a substrate CLEAR the recorded human approval
    id is re-presented here and **the agent executes** the merge through this
    same sanctioned transition (invariant 8 — approval is the gate, the agent
    is always the executor) — see :func:`assert_merge_preconditions`.
    """
    from teatree.core.models import MergeClear  # noqa: PLC0415

    if not isinstance(clear, MergeClear):
        msg = "merge_ticket_pr requires a MergeClear instance"
        raise MergePreconditionError(msg)

    slug = resolve_pr_repo_slug(clear)
    pr_id = clear.pr_id
    precheck = assert_merge_preconditions(
        clear=clear,
        executing_loop_identity=executing_loop_identity,
        slug=slug,
        pr_id=pr_id,
        human_authorized=human_authorized,
    )
    if precheck.needs_reconcile:
        # §928: a prior attempt's irreversible merge already landed; only
        # its post hook was lost. Do NOT re-issue the merge (GitHub would
        # 405 forever). Complete the transition idempotently against the
        # existing merge commit — the single-use CLEAR is still consumed
        # exactly once under the row lock in record_merge_and_advance, so
        # this neither double-merges nor weakens the replay defence.
        merged_sha = precheck.already_merged_sha
        reconciled = True
    else:
        merged_sha = execute_bound_merge(
            slug=slug,
            pr_id=pr_id,
            expected_head_oid=precheck.verified_sha,
        )
        reconciled = False
    checks = fetch_required_checks_status(slug, pr_id)
    state = record_merge_and_advance(
        clear=clear,
        merged_sha=merged_sha,
        required_checks_status=checks,
    )
    logger.info(
        "merge keystone: %s#%s %s at %s; ticket state=%s",
        slug,
        pr_id,
        "reconciled (lost post-hook recovered)" if reconciled else "merged",
        merged_sha[:8],
        state or "(no ticket)",
    )
    return MergeOutcome(pr_id=pr_id, slug=slug, merged_sha=merged_sha, ticket_state=state)
