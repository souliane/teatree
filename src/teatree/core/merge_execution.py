"""The missing ``t3`` merge FSM transition — loop-executes side (BLUEPRINT §17.4).

This is the keystone the factory was missing: the only sanctioned path from
``IN_REVIEW`` → ``MERGED``. Raw ``gh pr merge`` / ``glab mr merge`` bypasses the
ledger update, the HEAD/workstream attestation binding, the privacy/AI-signature
scan, and ``mark_merged()`` — leaving the FSM incoherent. The prohibition guard
(``hook_router._BLOCKED_COMMANDS``) mechanically refuses the raw path; this
module is the coherent replacement.

Transport dispatch (GitHub + GitLab) — the §17.4.3 fetch helpers
(``fetch_live_head_sha``, ``fetch_pr_is_draft``, ``fetch_required_checks_status``)
and ``execute_bound_merge`` dispatch on the CLEAR's host kind, resolved from the
ticket's ``issue_url`` (``github.com/...`` → GitHub, ``gitlab*/...`` → GitLab).
GitHub uses ``gh pr view`` / ``gh api PUT pulls/N/merge``; GitLab uses ``glab
api projects/<encoded>/merge_requests/<iid>`` and ``glab api -X PUT
.../merge``. A CLEAR without a resolvable ``issue_url`` defaults to GitHub for
back-compat. The host-kind switch is the only branch — every other §17.4.3
guard (substrate refusal, reviewer≠loop, SHA-bind, single-use replay) is shape
identical across forges. GitLab MRs still require colleague approval upstream;
this transport only makes the sanctioned ``t3 <overlay> ticket merge`` capable
of driving the merge once approvals are in.

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
from django_fsm import TransitionNotAllowed

from teatree.config import discover_overlays
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


def _run_glab(argv: list[str]) -> tuple[int, str, str]:
    """Sibling of :func:`_run_gh` for the GitLab transport.

    Mirrors the ``_run_gh`` shape — resolve the binary via ``shutil.which``,
    forward argv to :func:`run_allowed_to_fail`, return ``(rc, stdout,
    stderr)``. Kept intentionally thin so tests stub at this seam (the same
    seam ``_run_gh`` callers use) without depending on a live GitLab HTTP
    client or pass-resolved token.
    """
    glab = shutil.which("glab") or "glab"
    result = run_allowed_to_fail([glab, *argv], expected_codes=None)
    return result.returncode, result.stdout, result.stderr


def _resolve_host_kind(clear: object) -> str:
    """Return ``"github"`` or ``"gitlab"`` for *clear*'s PR transport.

    Resolution order:

    (1) the CLEAR's ``ticket.issue_url`` — ``github.com`` → ``"github"``,
        any URL whose hostname contains ``gitlab`` (gitlab.com or a
        self-hosted ``gitlab.<corp>`` host) → ``"gitlab"``.
    (2) default ``"github"`` — back-compat for CLEAR rows without a
        ticket / without a recognisable ``issue_url``. Pre-existing
        GitHub callers keep the legacy ``gh`` transport unchanged.

    The host kind is a transport-only switch; every §17.4.3 guard
    (substrate refusal, reviewer≠loop, SHA-bind, single-use replay)
    is identical across forges.
    """
    ticket = getattr(clear, "ticket", None)
    issue_url = str(getattr(ticket, "issue_url", "") or "") if ticket is not None else ""
    if not issue_url:
        return "github"
    host = urlparse(issue_url).hostname or ""
    host = host.lower()
    if "github.com" in host or host == "github":
        return "github"
    if "gitlab" in host:
        return "gitlab"
    return "github"


def _glab_project_path(slug: str) -> str:
    """URL-encode a project slug for ``glab api projects/<encoded>/...``.

    GitLab's REST API requires the project identifier ``group/repo`` (or
    ``group/subgroup/repo``) to be URL-encoded — the slashes become
    ``%2F``. Encoding is local to the GitLab branch so the GitHub
    transport keeps its raw ``owner/repo`` form.
    """
    return slug.replace("/", "%2F")


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


def _iter_candidate_repo_slugs() -> list[str]:
    """Every ``owner/repo`` reachable from this machine's overlay registry (#1335).

    Source set, de-duplicated preserving insertion order:

    (1) the running clone's ``origin`` (the same value
        :func:`_project_repo_slug` returns).
    (2) the ``origin`` slug of every registered overlay's ``project_path``
        (entry-point + TOML overlays via :func:`discover_overlays`).

    Used by :func:`_probe_candidate_repos` to recover from the #1335
    cross-repo confusion: a CLEAR issued from the teatree clone for a PR
    in a downstream overlay's repo (e.g. ``downstream-org/downstream-overlay#159``)
    used to resolve to ``souliane/teatree``'s same-numbered (unrelated)
    PR. With this enumeration the probe can verify each candidate and
    pick the one whose ``pulls/<N>`` head matches the reviewed SHA.

    Probe-side failures (``discover_overlays`` raises, a project path
    has no ``origin`` remote) are swallowed: the candidate set is best-
    effort, never load-bearing for the happy path.
    """
    seen: set[str] = set()
    candidates: list[str] = []

    def _add(slug: str) -> None:
        if slug and slug not in seen:
            seen.add(slug)
            candidates.append(slug)

    _add(_project_repo_slug())

    try:
        entries = discover_overlays()
    except Exception:  # noqa: BLE001 — overlay discovery is best-effort here
        entries = []
    for entry in entries:
        path = getattr(entry, "project_path", None)
        if path is None:
            continue
        try:
            slug = git.remote_slug(repo=str(path))
        except Exception:  # noqa: BLE001 — a missing remote must not block the probe
            slug = ""
        _add(slug)

    return candidates


def _reconcile_slug_against_reviewed_sha(
    *,
    initial_slug: str,
    pr_id: int,
    reviewed_sha: str,
    host_kind: str,
) -> str:
    """Pick the right repo when *initial_slug*'s PR doesn't carry *reviewed_sha* (#1335).

    The initial slug is what :func:`resolve_pr_repo_slug` returned: an
    explicit ``owner/repo`` from the CLEAR, the ticket's ``issue_url``
    repo, or — the #1335 trap — the running clone's ``origin`` for a
    CLEAR with no ticket and a non-``owner/repo`` slug. When that initial
    slug's PR head SHA matches *reviewed_sha* the merge proceeds against
    it unchanged (the common path). When the SHAs disagree, the same
    PR number may live in a downstream overlay's repo at the right SHA;
    the probe enumerates :func:`_iter_candidate_repo_slugs` and returns
    the first candidate whose ``pulls/<N>`` head matches.

    No reviewed SHA, no probe (back-compat with legacy callers that did
    not carry the SHA). No candidate match raises a
    :class:`MergePreconditionError` whose message names every candidate
    considered so the diagnosis is unambiguous — never the opaque "head
    moved" escalation that hid the #1335 bug.
    """
    if not reviewed_sha:
        return initial_slug
    initial_live = fetch_live_head_sha(initial_slug, pr_id, host_kind=host_kind)
    if initial_live == reviewed_sha:
        return initial_slug
    if not initial_live:
        # The forge call failed (missing credentials, network) or returned an
        # empty payload — that's a transient/auth condition, not a cross-repo
        # confusion. Defer to ``assert_merge_preconditions``, which raises the
        # established "could not resolve the live head" error against the
        # initial slug.
        return initial_slug
    candidates = _iter_candidate_repo_slugs()
    # The initial slug was already probed above — exclude it from the secondary
    # set so the candidates list in the error message reflects what was probed.
    other_candidates = [c for c in candidates if c != initial_slug]
    match = _probe_candidate_repos(
        pr_id=pr_id,
        reviewed_sha=reviewed_sha,
        candidates=other_candidates,
        host_kind=host_kind,
    )
    if match:
        logger.info(
            "merge_execution: cross-repo recovery for #%s — initial slug %r "
            "live=%s != reviewed=%s; probed %s, matched %r",
            pr_id,
            initial_slug,
            initial_live or "(unresolved)",
            reviewed_sha,
            other_candidates,
            match,
        )
        return match
    considered = [initial_slug, *other_candidates]
    msg = (
        f"PR head moved: live={initial_live or '(unresolved)'} != "
        f"reviewed={reviewed_sha} on the initial repo ({initial_slug!r}), and "
        f"no other candidate repo's PR #{pr_id} carries that SHA either. "
        f"Candidates considered: {considered}. This is either a genuine "
        f"force-push / new commits on the PR, or the CLEAR was issued from a "
        f"clone whose overlay registry doesn't include the target repo. "
        f"Re-escalate; the loop never self-issues a replacement "
        f"(§17.4.3 step 2 / #1335)."
    )
    raise MergePreconditionError(msg)


def _probe_candidate_repos(
    *,
    pr_id: int,
    reviewed_sha: str,
    candidates: list[str],
    host_kind: str,
) -> str:
    """Return the candidate ``owner/repo`` whose PR <pr_id> head == *reviewed_sha*.

    Iterates candidates in order and returns the first whose live head
    SHA matches *reviewed_sha* — the #1335 recovery path: when the
    initially-resolved repo's PR is an unrelated same-numbered PR, this
    finds the repo that actually owns the reviewed work. Returns ``""``
    when no candidate matches (a real force-push or a truly stale CLEAR).
    """
    for slug in candidates:
        if fetch_live_head_sha(slug, pr_id, host_kind=host_kind) == reviewed_sha:
            return slug
    return ""


def fetch_live_head_sha(slug: str, pr_id: int, *, host_kind: str = "github") -> str:
    """The PR/MR's current head SHA from the forge (never a branch ref) — §17.4.3 step 2.

    Dispatches on *host_kind*: GitHub uses ``gh pr view --json headRefOid``;
    GitLab uses ``glab api projects/<encoded>/merge_requests/<iid>`` and reads
    ``.sha`` off the JSON payload.
    """
    if host_kind == "gitlab":
        return _fetch_live_head_sha_gitlab(slug, pr_id)
    rc, out, _ = _run_gh(
        ["pr", "view", str(pr_id), "--repo", slug, "--json", "headRefOid", "--jq", ".headRefOid"],
    )
    return out.strip() if rc == 0 else ""


def _fetch_live_head_sha_gitlab(slug: str, pr_id: int) -> str:
    rc, out, _ = _run_glab(
        ["api", f"projects/{_glab_project_path(slug)}/merge_requests/{pr_id}"],
    )
    if rc != 0 or not out.strip():
        return ""
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("sha") or "")


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


def fetch_pr_merge_state(slug: str, pr_id: int, *, host_kind: str = "github") -> PrMergeState:
    """Whether the PR/MR is already merged, and at which commit — §928 reconciliation.

    A lost post-hook (process kill / DB lock / rollback between
    :func:`execute_bound_merge` and :func:`record_merge_and_advance`)
    leaves the PR merged on the forge while the CLEAR is still unconsumed
    and the FSM has not advanced. The retry must detect "already merged
    by us" and run the post hook idempotently rather than re-issuing the
    irreversible merge (which both forges refuse — GitHub 405, GitLab 405
    / 406 — a permanent brick) or failing the SHA precondition forever.
    Returns an empty state on any ``gh``/``glab`` error so the caller
    falls through to the normal (fail-closed) precondition path.

    GitHub reports ``state == "MERGED"`` and ``mergeCommit.oid``; GitLab
    reports ``state == "merged"`` and ``merge_commit_sha`` — normalised
    here to the same uppercase ``"MERGED"`` so ``PrMergeState.is_merged``
    works on both.
    """
    if host_kind == "gitlab":
        return _fetch_pr_merge_state_gitlab(slug, pr_id)
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


def _fetch_pr_merge_state_gitlab(slug: str, pr_id: int) -> PrMergeState:
    rc, out, _ = _run_glab(
        ["api", f"projects/{_glab_project_path(slug)}/merge_requests/{pr_id}"],
    )
    if rc != 0 or not out.strip():
        return PrMergeState(state="", merge_commit_oid="")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return PrMergeState(state="", merge_commit_oid="")
    if not isinstance(data, dict):
        return PrMergeState(state="", merge_commit_oid="")
    state = str(data.get("state") or "").upper()  # "merged" → "MERGED" (parity with GitHub)
    oid = str(data.get("merge_commit_sha") or data.get("squash_commit_sha") or "")
    return PrMergeState(state=state, merge_commit_oid=oid)


def fetch_pr_is_draft(slug: str, pr_id: int, *, host_kind: str = "github") -> bool:
    """Whether the PR/MR is in draft state — §17.4.3 step 4.

    Dispatches on *host_kind*: GitHub reads ``isDraft`` via ``gh pr view``;
    GitLab reads ``.draft`` from the ``glab api projects/<encoded>/
    merge_requests/<iid>`` payload (GitLab exposes both ``draft`` and the
    older ``work_in_progress`` field; the canonical form on modern
    versions is ``draft``).
    """
    if host_kind == "gitlab":
        return _fetch_pr_is_draft_gitlab(slug, pr_id)
    rc, out, _ = _run_gh(
        ["pr", "view", str(pr_id), "--repo", slug, "--json", "isDraft", "--jq", ".isDraft"],
    )
    return rc == 0 and out.strip().lower() == "true"


def _fetch_pr_is_draft_gitlab(slug: str, pr_id: int) -> bool:
    rc, out, _ = _run_glab(
        ["api", f"projects/{_glab_project_path(slug)}/merge_requests/{pr_id}"],
    )
    if rc != 0 or not out.strip():
        return False
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    # ``draft`` is canonical on modern GitLab; ``work_in_progress`` is the
    # legacy field kept for compatibility — accept either.
    return bool(data.get("draft") or data.get("work_in_progress"))


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


def fetch_required_checks_status(slug: str, pr_id: int, *, host_kind: str = "github") -> str:
    """Live required-checks rollup for the PR/MR head — §17.4.3 step 3.

    Evaluated against the forge's live rollup at merge time (the authoritative
    set), NOT the ``gh_verify_result`` snapshot saved on the CLEAR. Returns
    ``"green"`` only when every reported check concluded successfully;
    ``"pending"`` while any is still running; otherwise the failing state.

    Dispatches on *host_kind*: GitHub uses ``gh pr view --json
    statusCheckRollup``; GitLab uses ``glab api .../merge_requests/<iid>/
    pipelines`` (head pipeline status) — an MR with no pipeline at all is
    "green" (no required checks to satisfy), mirroring the GitHub
    rollup-empty path.
    """
    if host_kind == "gitlab":
        return _fetch_required_checks_status_gitlab(slug, pr_id)
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


_GITLAB_PIPELINE_GREEN_STATUSES = frozenset({"success", "manual", "skipped"})
_GITLAB_PIPELINE_PENDING_STATUSES = frozenset(
    {"pending", "running", "preparing", "scheduled", "waiting_for_resource", "created"},
)


def _classify_gitlab_pipeline(status: str) -> str:
    """Map a GitLab pipeline status string to ``green`` / ``pending`` / ``failed``.

    GitLab pipeline statuses (per the REST API documentation): ``created``,
    ``waiting_for_resource``, ``preparing``, ``pending``, ``running``,
    ``success``, ``failed``, ``canceled``, ``skipped``, ``manual``,
    ``scheduled``. ``success`` / ``manual`` / ``skipped`` are green;
    ``failed`` / ``canceled`` are failed; everything else is pending.
    """
    s = status.lower()
    if s in _GITLAB_PIPELINE_GREEN_STATUSES:
        return "green"
    if s in _GITLAB_PIPELINE_PENDING_STATUSES:
        return "pending"
    return "failed"


def _fetch_required_checks_status_gitlab(slug: str, pr_id: int) -> str:
    rc, out, _ = _run_glab(
        ["api", f"projects/{_glab_project_path(slug)}/merge_requests/{pr_id}/pipelines"],
    )
    if rc != 0:
        return "failed"
    try:
        pipelines = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        return "failed"
    if not isinstance(pipelines, list):
        return "failed"
    # MR with no pipeline at all => no required checks => green (mirrors
    # the GitHub empty-rollup branch).
    if not pipelines:
        return "green"
    # The head pipeline is the first entry (GitLab orders by id desc).
    head = pipelines[0]
    if not isinstance(head, dict):
        return "failed"
    return _classify_gitlab_pipeline(str(head.get("status") or ""))


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
    from teatree.core.models.merge_clear import is_non_reviewer_role  # noqa: PLC0415

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

    # The recorded reviewer verdict must be merge-safe. ``MergeClear.issue()``
    # rejects a non-green verdict at issue time, but a row written directly via
    # ``.objects.create()`` (fixture / migration / non-factory ORM path) could
    # smuggle a HOLD (pending/failed) verdict past it. Re-check here so the
    # live-CI re-check below can never stamp green over the reviewer's recorded
    # HOLD when CI self-flips green — the green-over-HOLD class (§17.8 clause 3:
    # the checker's recorded verdict is authoritative, mirroring the
    # ``is_non_reviewer_role`` issue/merge double-guard above).
    if clear.gh_verify_result != clear.VerifyResult.GREEN:
        msg = (
            f"MergeClear for {slug}#{pr_id} records gh_verify_result "
            f"({clear.gh_verify_result!r}), not green — the reviewer recorded a HOLD at the "
            f"reviewed tree; a non-green verdict can never authorize a merge regardless of the "
            f"live CI rollup (§17.4.2 / §17.8 clause 3)"
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

    # The factory ``MergeClear.issue()`` rejects a maker/coding-agent/loop
    # reviewer_identity at issue time (§17.8 clause 3 — the same shared
    # ``is_non_reviewer_role`` helper), but a row written directly via
    # ``.objects.create()`` (fixture, migration, or any non-factory ORM
    # path — e.g. ``ticket.py`` loads the row by pk without re-validation)
    # would otherwise smuggle a self-attesting maker through the equality
    # check above. Re-check the same role classification here so the
    # issue-time and merge-time gates cannot drift apart (codex #1282
    # finding 1 / #1283).
    if is_non_reviewer_role(clear.reviewer_identity):
        msg = (
            f"MergeClear reviewer_identity ({clear.reviewer_identity!r}) is a "
            f"maker/coding-agent/loop non-reviewer role — a CLEAR must be issued "
            f"by an independent cold reviewer, not self-attested (§17.8 clause 3)"
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


def _reconcile_if_already_merged(
    *,
    slug: str,
    pr_id: int,
    live_sha: str,
    host_kind: str = "github",
) -> "MergePrecheck | None":
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
    merge_state = fetch_pr_merge_state(slug, pr_id, host_kind=host_kind)
    if not merge_state.is_merged:
        return None
    return MergePrecheck(
        verified_sha=live_sha,
        already_merged_sha=merge_state.merge_commit_oid or live_sha,
    )


def assert_merge_preconditions(  # noqa: PLR0913 — §17.4.3 gate entry-point; each kwarg is a documented step input.
    *,
    clear: object,
    executing_loop_identity: str,
    slug: str,
    pr_id: int,
    human_authorized: str = "",
    host_kind: str = "github",
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
    live_sha = fetch_live_head_sha(slug, pr_id, host_kind=host_kind)
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

    reconcile = _reconcile_if_already_merged(
        slug=slug,
        pr_id=pr_id,
        live_sha=live_sha,
        host_kind=host_kind,
    )
    if reconcile is not None:
        return reconcile

    # 4. Not draft.
    if fetch_pr_is_draft(slug, pr_id, host_kind=host_kind):
        msg = f"{slug}#{pr_id} is in draft state — refusing to merge (§17.4.3 step 4)"
        raise MergePreconditionError(msg)

    # 3. CI still green — against the forge's LIVE rollup, not the saved snapshot.
    checks = fetch_required_checks_status(slug, pr_id, host_kind=host_kind)
    if checks != "green":
        msg = (
            f"live required-checks for {slug}#{pr_id} are {checks!r}, not green — "
            f"refusing to merge (§17.4.3 step 3; the live list is the source of "
            f"truth, not the CLEAR snapshot)"
        )
        raise MergePreconditionError(msg)

    return MergePrecheck(verified_sha=live_sha)


def execute_bound_merge(
    *,
    slug: str,
    pr_id: int,
    expected_head_oid: str,
    host_kind: str = "github",
) -> str:
    """Squash-merge bound to ``expected_head_oid`` — fail closed on head drift.

    GitHub: ``PUT repos/<slug>/pulls/<n>/merge`` with ``sha=<oid>``.
    GitLab: ``PUT projects/<encoded>/merge_requests/<iid>/merge`` with
    ``sha=<oid>`` (GitLab enforces the SHA-bind upstream and 409s on drift).

    If the forge reports the head moved, the merge is refused and raised
    as :class:`MergeHeadMovedError` — a failed check, never a
    retry-with-new-head (§17.4.3 "bind execution to the exact verified
    SHA, fail closed").
    """
    if host_kind == "gitlab":
        return _execute_bound_merge_gitlab(slug=slug, pr_id=pr_id, expected_head_oid=expected_head_oid)
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
            # Print the full ``expected_head_oid`` so a length mismatch can never
            # masquerade as a value mismatch (#1162).
            msg = (
                f"GitHub refused the merge of {slug}#{pr_id}: head moved off "
                f"{expected_head_oid} (length={len(expected_head_oid)}, "
                f"expected_head_oid mismatch). Treated as a failed check — "
                f"NOT retried with a new head (§17.4.3)"
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


def _execute_bound_merge_gitlab(*, slug: str, pr_id: int, expected_head_oid: str) -> str:
    endpoint = f"projects/{_glab_project_path(slug)}/merge_requests/{pr_id}/merge"
    rc, out, err = _run_glab(
        [
            "api",
            "-X",
            "PUT",
            endpoint,
            "-f",
            f"sha={expected_head_oid}",
            "-f",
            "squash=true",
        ],
    )
    if rc != 0:
        combined = f"{out}\n{err}".lower()
        if "sha" in combined and ("does not match" in combined or "409" in combined or "conflict" in combined):
            msg = (
                f"GitLab refused the merge of {slug}!{pr_id}: head moved off "
                f"{expected_head_oid} (length={len(expected_head_oid)}, "
                f"expected_head_oid mismatch). Treated as a failed check — "
                f"NOT retried with a new head (§17.4.3)"
            )
            raise MergeHeadMovedError(msg)
        msg = f"merge of {slug}!{pr_id} failed: {err.strip() or out.strip() or 'glab api non-zero'}"
        raise MergePreconditionError(msg)
    try:
        merged = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        merged = {}
    if not isinstance(merged, dict):
        return expected_head_oid
    # GitLab returns ``merge_commit_sha`` (squashed merge commit) or ``sha``
    # depending on merge_method; prefer the dedicated commit field.
    merged_sha = str(merged.get("merge_commit_sha") or merged.get("sha") or "")
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
        # #1343: state-complete reconcile. An authorised, audited PR-merge
        # is the authority — every pre-merged state (NOT_STARTED through
        # IN_REVIEW, plus SHIPPED) must advance to MERGED. RETROSPECTED/
        # DELIVERED are past MERGED and stay where they are; IGNORED is
        # abandoned. The original ``state in {in_review, merged}`` guard
        # left STARTED tickets visibly stuck on the statusline after their
        # PR merged (#1324 follow-up). The FSM source-set on
        # ``reconcile_merged`` is the single source of truth — catching
        # ``TransitionNotAllowed`` lets the source list evolve in one
        # place (the model) without a parallel guard here.
        try:
            ticket.reconcile_merged()
        except TransitionNotAllowed:
            logger.info(
                "merge keystone: ticket %s state=%s is past MERGED; FSM unchanged",
                ticket.pk,
                ticket.state,
            )
        else:
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
    host_kind = _resolve_host_kind(clear)
    slug = _reconcile_slug_against_reviewed_sha(
        initial_slug=slug,
        pr_id=pr_id,
        reviewed_sha=str(getattr(clear, "reviewed_sha", "") or ""),
        host_kind=host_kind,
    )
    precheck = assert_merge_preconditions(
        clear=clear,
        executing_loop_identity=executing_loop_identity,
        slug=slug,
        pr_id=pr_id,
        human_authorized=human_authorized,
        host_kind=host_kind,
    )
    if precheck.needs_reconcile:
        # §928: a prior attempt's irreversible merge already landed; only
        # its post hook was lost. Do NOT re-issue the merge (the forge
        # would 405 forever). Complete the transition idempotently against
        # the existing merge commit — the single-use CLEAR is still
        # consumed exactly once under the row lock in
        # record_merge_and_advance, so this neither double-merges nor
        # weakens the replay defence.
        merged_sha = precheck.already_merged_sha
        reconciled = True
    else:
        merged_sha = execute_bound_merge(
            slug=slug,
            pr_id=pr_id,
            expected_head_oid=precheck.verified_sha,
            host_kind=host_kind,
        )
        reconciled = False
    checks = fetch_required_checks_status(slug, pr_id, host_kind=host_kind)
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
