"""The missing ``t3`` merge FSM transition — loop-executes side (BLUEPRINT §17.4).

This is the keystone the factory was missing: the only sanctioned path from
``IN_REVIEW`` → ``MERGED``. Raw ``gh pr merge`` / ``glab mr merge`` bypasses the
ledger update, the HEAD/workstream attestation binding, the privacy/AI-signature
scan, and ``mark_merged()`` — leaving the FSM incoherent. The prohibition guard
(``hook_router._BLOCKED_COMMANDS``) mechanically refuses the raw path; this
module is the coherent replacement.

Transport dispatch (GitHub + GitLab) — the §17.4.3 fetch helpers
(``fetch_live_head_sha``, ``fetch_pr_is_draft``, ``fetch_required_checks_status``)
and ``execute_bound_merge`` delegate to a :class:`CodeHostBackend` resolved on
the CLEAR's host kind (from the ticket's ``issue_url``: ``github.com/...`` →
GitHub, ``gitlab*/...`` → GitLab) through ``core.backend_registry`` — core never
imports ``teatree.backends`` (§17.6.2). The gh/glab argv lives on the backend
impls; this module keeps every verdict / transient / head-moved / policy-refusal
classification and the exact error f-strings, so the keystone error parity is
unchanged while the transport is the backend's. The residual host-kind switch
selects only the verdict classifier (GitHub rollup vs GitLab pipeline), never the
transport. A CLEAR without a resolvable ``issue_url`` defaults to GitHub for
back-compat. GitLab MRs still require colleague approval upstream; this transport
only makes the sanctioned ``t3 <overlay> ticket merge`` capable of driving the
merge once approvals are in.

Flow (orchestrator-decides / loop-executes, §17.4.1):

Pre-condition hook — ``assert_merge_preconditions`` runs the loop's §17.4.3
validation in order: a valid, actionable ``MergeClear`` row re-read from the
DB; CI green on the exact PR head; an independent cold-review CLEAR recorded
(a ``reviewer_identity`` distinct from the executing loop — §17.8 clause 3);
plus the §17.4.3 SHA-match and not-draft checks. ``substrate`` blast-class PRs
need a per-PR human sign-off — a matching ``human_authorizer`` OR the overlay
standing at ``autonomy = full`` (invariant 4 carve-out / §17.4.3 step 5).

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

Transient-response retry (#1813) — a sibling window to #928. The forge merge
call can fail with a TRUNCATED/empty body (``unexpected end of JSON input``),
a network error, a timeout, or a 5xx — the forge momentarily failing to
answer, NOT a verdict on the merge (the #1804 stranding: a cleared PR left
OPEN with a consumed-by-nothing CLEAR after a truncated merge response).
``execute_bound_merge`` classifies such a response as transient
(:func:`_is_transient_merge_response`, distinct from a policy refusal and from
a head-moved) and auto-retries a bounded number of times
(:data:`MERGE_TRANSIENT_ATTEMPTS`, exponential backoff) before raising
:class:`MergeTransientError`. The single-use CLEAR stays idempotently
reusable: a transient failure raises BEFORE the post hook, so the CLEAR is
never consumed and a retry of the SAME CLEAR can merge. Before each retry the
PR's merge state is re-probed — a transient response whose merge ACTUALLY
LANDED reconciles via the #928 path (the existing merge commit is returned and
the idempotent post hook runs) rather than re-issuing a merge the forge would
now 405. A policy refusal (not-mergeable / required-checks / 405 / 422) and a
head-moved are never retried — they are verdicts, not transient failures.
"""

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict, cast
from urllib.parse import urlparse

from django.apps import apps
from django.db import transaction
from django.utils import timezone
from django_fsm import TransitionNotAllowed

from teatree.config import discover_overlays
from teatree.core.backend_protocols import ForgeMergeResult, PrMergeState, rollup_query_failed
from teatree.core.backend_registry import get_backend_provider
from teatree.project import find_project_root
from teatree.utils import git
from teatree.utils.url_slug import slug_from_issue_or_pr_url

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend
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


class MergeTransientError(MergePreconditionError):
    """The forge merge call failed with a transient/empty-JSON/network/5xx response.

    Distinct from a policy refusal (not-mergeable / required-checks /
    review-required) and from a head-moved (:class:`MergeHeadMovedError`):
    a truncated or empty API body (``unexpected end of JSON input``), a
    network error, a timeout, or a 5xx is the forge momentarily failing to
    answer, NOT a verdict on the merge. ``execute_bound_merge`` auto-retries
    a bounded number of times before raising this; only after the retries are
    exhausted does it surface so the caller re-escalates into the durable
    backlog. Because it is raised BEFORE the post hook, the single-use CLEAR
    is never consumed — a manual / loop retry of the SAME CLEAR can merge
    (the #1804 stranding window).
    """


MERGE_TRANSIENT_ATTEMPTS = 3
MERGE_TRANSIENT_BASE_DELAY = 0.5

# Lower-cased substrings that mark a forge merge response as TRANSIENT — the
# forge momentarily failing to answer rather than refusing the merge. A
# truncated/empty JSON body (the #1804 window), a network/connection error, a
# timeout, or a 5xx. Matched against the combined stdout+stderr.
_TRANSIENT_MERGE_MARKERS = (
    "unexpected end of json input",
    "unexpected eof",
    "empty response",
    "connection reset",
    "connection refused",
    "connection closed",
    "broken pipe",
    "timeout",
    "timed out",
    "eof",
    "i/o timeout",
    "temporary failure",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "502",
    "503",
    "504",
)

# Lower-cased substrings that mark a forge merge response as a POLICY REFUSAL —
# a verdict on the merge, never retried. Checked first so a refusal that also
# mentions a transient-looking token (rare) is still classified as a refusal.
_POLICY_REFUSAL_MERGE_MARKERS = (
    "not mergeable",
    "is not mergeable",
    "required status check",
    "review required",
    "changes requested",
    "merge conflict",
    "405",
    "422",
)


def _is_transient_merge_response(rc: int, out: str, err: str) -> bool:
    """True iff a non-zero forge merge response is transient (retryable).

    A policy refusal (not-mergeable / required-checks / 405 / 422) is never
    transient — checked first so a refusal is never mis-retried. An empty
    body with no recognisable marker (rc != 0, no stdout, no stderr) is the
    truncated/dropped-response shape and is treated as transient. Anything
    else with an explicit non-transient message is NOT transient.
    """
    if rc == 0:
        return False
    combined = f"{out}\n{err}".lower()
    if any(marker in combined for marker in _POLICY_REFUSAL_MERGE_MARKERS):
        return False
    if any(marker in combined for marker in _TRANSIENT_MERGE_MARKERS):
        return True
    return not combined.strip()


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


def _code_host_for(host_kind: str) -> "CodeHostBackend":
    """The merge-transport backend for *host_kind*, resolved via the registry.

    Core never imports ``teatree.backends`` (the §17.6.2 ``core ↛ backends``
    edge); it reaches a built backend ONLY through
    :func:`core.backend_registry.get_backend_provider`. The token/base_url are
    left empty — the merge-RPC runners use ambient ``gh``/``glab`` auth, the
    same as the former in-module ``_run_gh``/``_run_glab`` did. When the
    backends app is not installed the provider is the fail-safe
    ``_UnconfiguredProvider``, whose ``build_*`` RAISE a clear ``RuntimeError``
    (loud-failure: a merge in an unconfigured context fails visibly rather than
    silently shelling out).
    """
    provider = get_backend_provider()
    if host_kind == "gitlab":
        return provider.build_gitlab_host(token="", base_url="")
    return provider.build_github_host(token="")


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

    Delegates to the registry-resolved :class:`CodeHostBackend`
    (:func:`_code_host_for`); the gh/glab argv lives in the backend.
    """
    return _code_host_for(host_kind).fetch_live_head_sha(slug=slug, pr_id=pr_id)


def fetch_pr_merge_state(slug: str, pr_id: int, *, host_kind: str = "github") -> PrMergeState:
    """Whether the PR/MR is already merged, and at which commit — §928 reconciliation.

    A lost post-hook (process kill / DB lock / rollback between
    :func:`execute_bound_merge` and :func:`record_merge_and_advance`)
    leaves the PR merged on the forge while the CLEAR is still unconsumed
    and the FSM has not advanced. The retry must detect "already merged
    by us" and run the post hook idempotently rather than re-issuing the
    irreversible merge (which both forges refuse — GitHub 405, GitLab 405
    / 406 — a permanent brick) or failing the SHA precondition forever.
    Returns an empty state on any forge error so the caller falls through to
    the normal (fail-closed) precondition path. The backend normalises both
    forges' state to the uppercase ``"MERGED"`` ``PrMergeState.is_merged`` reads.
    """
    return _code_host_for(host_kind).fetch_pr_merge_state(slug=slug, pr_id=pr_id)


def fetch_pr_is_draft(slug: str, pr_id: int, *, host_kind: str = "github") -> bool:
    """Whether the PR/MR is in draft state — §17.4.3 step 4.

    Delegates to the registry-resolved :class:`CodeHostBackend`; GitLab reads
    ``draft``/``work_in_progress`` and GitHub ``isDraft`` inside the backend.
    """
    return _code_host_for(host_kind).fetch_pr_is_draft(slug=slug, pr_id=pr_id)


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
    """Live required-checks verdict for the PR/MR head — §17.4.3 step 3.

    Evaluated against the forge's live rollup at merge time (the authoritative
    set), NOT the ``gh_verify_result`` snapshot saved on the CLEAR. Returns
    ``"green"`` only when every reported check concluded successfully;
    ``"pending"`` while any is still running; otherwise the failing state.

    The backend returns the RAW rollup (GitHub ``statusCheckRollup`` entries,
    GitLab pipeline entries); core does the verdict classification here so the
    §17.4.3 ``green``/``pending``/``failed`` semantics stay in one place. A
    backend query failure surfaces as the :data:`ROLLUP_QUERY_FAILED` sentinel
    → ``failed``; an empty rollup means no required checks → ``green``. GitLab
    needs the head SHA to pick the right (non-merge-train) pipeline, fetched via
    :func:`fetch_live_head_sha`.
    """
    backend = _code_host_for(host_kind)
    rollup = backend.fetch_required_checks_rollup(slug=slug, pr_id=pr_id)
    if rollup_query_failed(rollup):
        return "failed"
    if host_kind == "gitlab":
        if not rollup:
            return "green"
        head_sha = backend.fetch_live_head_sha(slug=slug, pr_id=pr_id)
        head = _select_gitlab_head_pipeline(list(rollup), head_sha, slug=slug, pr_id=pr_id)
        if head is None:
            return "failed"
        return _classify_gitlab_pipeline(str(head.get("status") or ""))
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


class _GitlabPipeline(TypedDict, total=False):
    """One entry of ``glab api .../merge_requests/<iid>/pipelines``."""

    id: object
    sha: object
    ref: object
    source: object
    status: object


def _is_merge_train_pipeline(pipeline: _GitlabPipeline) -> bool:
    ref = str(pipeline.get("ref") or "")
    source = str(pipeline.get("source") or "")
    return source == "merge_train" or "/train" in ref


def _select_gitlab_head_pipeline(
    pipelines: list[object],
    head_sha: str,
    *,
    slug: str,
    pr_id: int,
) -> _GitlabPipeline | None:
    """Pick the pipeline for the MR head commit, ignoring merge-train pipelines.

    The ``…/merge_requests/<iid>/pipelines`` endpoint interleaves merge-train
    pipelines (each on a transient train SHA, often canceled the moment the
    train re-bases) ahead of the real head-branch pipeline, so ``pipelines[0]``
    is not reliably the head pipeline. Match on the MR head SHA instead. When
    the head SHA is known but no pipeline matches it, the head commit has no
    pipeline of its own — return ``None`` so the caller fails closed rather
    than reading an unrelated commit's pipeline. The newest non-train pipeline
    is used only when the head SHA could not be fetched at all.
    """
    entries = [cast("_GitlabPipeline", p) for p in pipelines if isinstance(p, dict)]
    candidates = [e for e in entries if not _is_merge_train_pipeline(e)]
    if head_sha:
        for pipeline in candidates:
            if str(pipeline.get("sha") or "") == head_sha:
                return pipeline
        logger.info(
            "merge_execution: no GitLab pipeline matches MR head %s for %s#%s "
            "(non-train candidates: %s) — failing closed",
            head_sha,
            slug,
            pr_id,
            [str(p.get("sha") or "") for p in candidates],
        )
        return None
    logger.info(
        "merge_execution: GitLab MR head SHA unavailable for %s#%s — falling back to newest non-train pipeline",
        slug,
        pr_id,
    )
    return candidates[0] if candidates else None


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

    # 5. blast_class respected — substrate-class PRs are draft-locked and
    #    require a recorded human sign-off (invariant 4 / §17.4.3 step 5). Two
    #    things satisfy the per-PR sign-off, in this order:
    #      a. a per-CLEAR ``human_authorizer`` matching the value re-presented
    #         at merge time (the owner approved this exact diff), OR
    #      b. the overlay's STANDING autonomy grant resolving to ``full`` — the
    #         owner recorded once, in config, that this overlay merges
    #         end-to-end without a per-PR sign-off (invariant 4 carve-out).
    #    Either way the AGENT executes through this same SHA-bound, audited
    #    transition (invariant 8) — never raw ``gh``, never a human-performed
    #    merge. The quality/safety floor (independent cold-review, reviewed-SHA
    #    bind, CI-green, not-draft, never-lockout, privacy scan) is untouched by
    #    the carve-out; autonomy=full removes ONLY the per-PR human sign-off.
    if (
        clear.is_substrate()
        and not clear.human_merge_authorized_by(presented)
        and not _overlay_grants_full_substrate_autonomy(clear)
    ):
        detail = (
            "no human authoriser recorded on the CLEAR and the overlay autonomy is not full"
            if not clear.human_authorizer
            else f"presented authoriser != recorded ({clear.human_authorizer!r})"
            if presented
            else "no --human-authorized presented at merge time and the overlay autonomy is not full"
        )
        msg = (
            f"MergeClear for {slug}#{pr_id} is blast_class=substrate — substrate "
            f"changes require a recorded human approval and are draft-locked "
            f"(invariant 4); the loop never auto-merges them (§17.4.3 step 5). "
            f"{detail.capitalize()}. The sanctioned paths: `t3 <overlay> autonomy "
            f"set full` (the standing owner grant), or issue `t3 <overlay> ticket "
            f"clear … --blast-class substrate --human-authorize <id>` (a per-PR "
            f"recorded approval), then the agent executes `t3 <overlay> ticket "
            f"merge <clear_id> [--human-authorized <id>]`"
        )
        raise MergePreconditionError(msg)

    return clear


def _resolve_clear_overlay_name(clear: "MergeClear") -> str:
    """The overlay name to resolve autonomy against for *clear*.

    The CLEAR's ``ticket.overlay`` is authoritative when present, but the loop
    routinely issues a CLEAR with no linked ticket (every substrate CLEAR in
    the live ledger). The CLEAR always carries the ``owner/repo`` ``slug``, so
    the overlay is recovered from it via :func:`infer_overlay_for_url` — the
    same workspace-repos inference ``ticket.overlay`` itself is populated from.
    Returns ``""`` when neither source resolves an overlay.
    """
    from teatree.core.overlay_loader import infer_overlay_for_url  # noqa: PLC0415

    overlay_name = str(getattr(getattr(clear, "ticket", None), "overlay", "") or "").strip()
    if overlay_name:
        return overlay_name
    return infer_overlay_for_url(str(getattr(clear, "slug", "") or "")).strip()


def _overlay_grants_full_substrate_autonomy(clear: "MergeClear") -> bool:
    """Whether the CLEAR's overlay stands at ``autonomy = full`` (invariant 4 carve-out).

    Resolves the effective autonomy for the CLEAR's overlay
    (:func:`_resolve_clear_overlay_name`) via :func:`get_effective_settings`.
    ``full`` is the owner's standing, recorded grant that this overlay merges
    end-to-end without a per-PR human sign-off; it satisfies the substrate
    sign-off in place of a per-CLEAR ``human_authorizer``. Any other tier
    (``notify`` / ``babysit``), or an unresolvable overlay, is fail-closed:
    the per-CLEAR human authoriser stays mandatory. The carve-out touches ONLY
    the per-PR sign-off — every other substrate-merge floor guard runs unchanged.
    """
    from teatree.config import Autonomy, get_effective_settings  # noqa: PLC0415

    overlay_name = _resolve_clear_overlay_name(clear)
    if not overlay_name:
        return False
    return get_effective_settings(overlay_name=overlay_name).autonomy is Autonomy.FULL


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


def _assert_anti_vacuity(clear: "MergeClear", head_sha: str) -> None:
    """Refuse a merge whose CLEAR ticket lacks a SHA-bound anti-vacuity proof (#1829).

    NO-OP when ``require_anti_vacuity_attestation`` is off (opt-in default) or
    the CLEAR carries no ticket (the attestation lives on the ticket's durable
    ``extra``). The :class:`AntiVacuityAttestationError` raised on a block is
    re-wrapped as a :class:`MergePreconditionError` so the merge command's
    single re-escalation path surfaces it (the loop never self-issues a
    replacement CLEAR).
    """
    from teatree.core.anti_vacuity_gate import (  # noqa: PLC0415
        AntiVacuityAttestationError,
        check_anti_vacuity_attestation,
    )

    ticket = clear.ticket
    if ticket is None:
        return
    try:
        check_anti_vacuity_attestation(ticket, head_sha, transition="merge")
    except AntiVacuityAttestationError as exc:
        raise MergePreconditionError(str(exc)) from exc


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

    The substrate sign-off (step 5) is satisfied by EITHER a matching per-CLEAR
    ``human_authorized`` OR the CLEAR's overlay standing at ``autonomy = full``.
    ``human_authorized`` unlocks the merge **only** when the CLEAR is
    substrate-class AND its recorded ``human_authorizer`` matches; it can never
    unlock a non-substrate CLEAR. The ``autonomy = full`` carve-out is the
    owner's standing, recorded grant that the overlay merges end-to-end without
    a per-PR sign-off (invariant 4) — every other tier keeps the per-CLEAR
    authoriser mandatory. Either path runs the identical sanctioned ``t3``
    transition (invariant 8: even an owner-approved merge goes through this
    transition, never raw ``gh`` and never a human-performed merge action). The
    carve-out removes ONLY the per-PR sign-off — the quality/safety floor
    (independent cold-review, reviewed-SHA bind, CI-green, not-draft,
    never-lockout, privacy scan) is untouched.
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

    # §17.4.3 + #1829: bound to the just-verified ``live_sha`` so a force-push
    # invalidates the CLEAR and the attestation together (see _assert_anti_vacuity).
    _assert_anti_vacuity(authorized_clear, live_sha)

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

    A transient/empty-JSON/network/5xx forge response (#1813 — the #1804
    ``unexpected end of JSON input`` window) is the forge momentarily
    failing to answer, NOT a verdict: it is auto-retried up to
    :data:`MERGE_TRANSIENT_ATTEMPTS` times with exponential backoff before
    raising :class:`MergeTransientError`. Because the failure is raised
    BEFORE the post hook, the single-use CLEAR is never consumed — a retry
    of the SAME CLEAR can merge. Before each retry the PR's merge state is
    re-probed: a transient response whose merge ACTUALLY LANDED at the
    bound SHA returns the existing merge commit so the caller runs the post
    hook idempotently instead of re-issuing the (then-405-bricking) merge.
    A policy refusal (not-mergeable / required-checks / 405 / 422) and a
    head-moved are NOT transient — they raise on the first attempt.
    """
    for attempt in range(MERGE_TRANSIENT_ATTEMPTS):
        if attempt > 0:
            landed = _already_merged_at(
                slug=slug,
                pr_id=pr_id,
                expected_head_oid=expected_head_oid,
                host_kind=host_kind,
            )
            if landed:
                return landed
            time.sleep(MERGE_TRANSIENT_BASE_DELAY * (2 ** (attempt - 1)))
        try:
            return _attempt_bound_merge(
                slug=slug,
                pr_id=pr_id,
                expected_head_oid=expected_head_oid,
                host_kind=host_kind,
            )
        except MergeTransientError as exc:
            if attempt == MERGE_TRANSIENT_ATTEMPTS - 1:
                raise
            logger.info(
                "merge_execution: transient forge response on merge attempt %d/%d for %s#%s — %s",
                attempt + 1,
                MERGE_TRANSIENT_ATTEMPTS,
                slug,
                pr_id,
                exc,
            )
    msg = f"merge of {slug}#{pr_id} exhausted {MERGE_TRANSIENT_ATTEMPTS} transient retries"  # pragma: no cover
    raise MergeTransientError(msg)  # pragma: no cover — the final attempt re-raises before the loop can fall through


def _already_merged_at(*, slug: str, pr_id: int, expected_head_oid: str, host_kind: str) -> str:
    """The existing merge commit when the PR/MR is ALREADY merged at ``expected_head_oid``.

    A transient response may mask a merge that actually LANDED on the forge
    (the body was truncated, not the action). Re-probing before the next
    retry detects that and returns the existing merge commit (or the bound
    SHA when the forge exposes no merge-commit oid), so the caller runs the
    idempotent post hook rather than re-issuing a merge the forge would now
    405. Returns ``""`` when the PR/MR is not (yet) merged.
    """
    merge_state = fetch_pr_merge_state(slug, pr_id, host_kind=host_kind)
    if not merge_state.is_merged:
        return ""
    return merge_state.merge_commit_oid or expected_head_oid


def _attempt_bound_merge(*, slug: str, pr_id: int, expected_head_oid: str, host_kind: str) -> str:
    """One bound-merge attempt; raises :class:`MergeTransientError` on a retryable response.

    The backend's :meth:`CodeHostBackend.merge_pr_squash_bound` runs the
    PUT and returns the raw :class:`ForgeMergeResult`; core classifies it
    (head-moved / transient / policy refusal) and raises the typed error with
    the forge-specific f-string here, so the byte-for-byte error parity the
    keystone tests pin is unchanged while the transport lives in the backend.
    """
    result = _code_host_for(host_kind).merge_pr_squash_bound(
        slug=slug,
        pr_id=pr_id,
        expected_head_oid=expected_head_oid,
    )
    if result.returncode != 0:
        _raise_bound_merge_failure(
            result=result,
            slug=slug,
            pr_id=pr_id,
            expected_head_oid=expected_head_oid,
            host_kind=host_kind,
        )
    return result.merged_sha or expected_head_oid


def _raise_bound_merge_failure(
    *,
    result: ForgeMergeResult,
    slug: str,
    pr_id: int,
    expected_head_oid: str,
    host_kind: str,
) -> None:
    """Classify a non-zero merge response and raise the typed forge-specific error.

    GitLab and GitHub have distinct head-moved sniffs and distinct error
    f-strings (``!`` vs ``#``, ``glab`` vs ``gh``); both are preserved verbatim.
    """
    out, err = result.stdout, result.stderr
    combined = f"{out}\n{err}".lower()
    if host_kind == "gitlab":
        if "sha" in combined and ("does not match" in combined or "409" in combined or "conflict" in combined):
            msg = (
                f"GitLab refused the merge of {slug}!{pr_id}: head moved off "
                f"{expected_head_oid} (length={len(expected_head_oid)}, "
                f"expected_head_oid mismatch). Treated as a failed check — "
                f"NOT retried with a new head (§17.4.3)"
            )
            raise MergeHeadMovedError(msg)
        if _is_transient_merge_response(result.returncode, out, err):
            msg = (
                f"merge of {slug}!{pr_id} hit a transient forge response: "
                f"{err.strip() or out.strip() or 'empty glab api response'} — retrying (#1813)"
            )
            raise MergeTransientError(msg)
        msg = f"merge of {slug}!{pr_id} failed: {err.strip() or out.strip() or 'glab api non-zero'}"
        raise MergePreconditionError(msg)
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
    if _is_transient_merge_response(result.returncode, out, err):
        msg = (
            f"merge of {slug}#{pr_id} hit a transient forge response: "
            f"{err.strip() or out.strip() or 'empty gh api response'} — retrying (#1813)"
        )
        raise MergeTransientError(msg)
    msg = f"merge of {slug}#{pr_id} failed: {err.strip() or out.strip() or 'gh api non-zero'}"
    raise MergePreconditionError(msg)


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

    The atomic block is wrapped in :func:`retry_on_locked` (#1520): a transient
    ``database is locked`` from a concurrent canonical-DB writer must not abort
    the merge keystone mid-flight. A retry re-opens the transaction, re-reads
    the CLEAR ``select_for_update``-locked, and re-asserts the single-use
    guard, so it consumes the CLEAR exactly once and never double-merges (the
    irreversible GitHub merge already ran before this hook; only this
    idempotent DB write retries).
    """
    from teatree.core.db_retry import retry_on_locked  # noqa: PLC0415
    from teatree.core.models import MergeClear  # noqa: PLC0415

    if not isinstance(clear, MergeClear):  # pragma: no cover - guarded by caller
        msg = "record_merge_and_advance requires a MergeClear instance"
        raise MergePreconditionError(msg)

    merge_audit_model = apps.get_model("core", "MergeAudit")

    def _consume_and_advance() -> str:
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

    return retry_on_locked(_consume_and_advance)


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
