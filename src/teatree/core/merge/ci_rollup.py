"""Forge transport resolution + CI/pipeline verdict classification (GitHub + GitLab).

The lowest layer of the ``core/merge`` package: ``_code_host_for`` resolves the
merge-transport backend via ``core.backend_registry`` (core never imports
``teatree.backends`` — the §17.6.2 ``core ↛ backends`` edge), and the three thin
``fetch_*`` delegators plus the rollup/pipeline classifiers live here so that
both ``pr_slug_resolution`` (which probes live head SHAs) and ``execution``
(which re-checks CI at merge time) depend DOWN on this module — the §1993 cut
that keeps the intra-package DAG acyclic under ``forbid_circular_dependencies``.
"""

import logging
from typing import TYPE_CHECKING, TypedDict, cast

from teatree.core.backend_protocols import PrMergeState, rollup_query_failed
from teatree.core.backend_registry import get_backend_provider
from teatree.core.models import MergeClear

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend
    from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)


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


def fetch_pr_author(slug: str, pr_id: int, *, host_kind: str = "github") -> str:
    """The PR/MR author handle — the §17.4.3 author-gate input (#1773).

    Delegates to the registry-resolved :class:`CodeHostBackend`; GitHub reads
    ``author.login`` and GitLab ``.author.username`` inside the backend. Returns
    ``""`` on any forge error so the keystone's author gate fails closed.
    """
    return _code_host_for(host_kind).fetch_pr_author(slug=slug, pr_id=pr_id)


def fetch_pr_changed_paths(slug: str, pr_id: int, *, host_kind: str = "github") -> list[str]:
    """The PR/MR's changed file paths — feeds the path-based substrate detector.

    Delegates to the registry-resolved :class:`CodeHostBackend` (GitHub reads
    ``gh pr view --json files``; GitLab the MR ``diffs`` API). A forge error
    degrades to an empty list — the path detector is an ADD-ON to the recorded
    ``blast_class`` label (it can only widen substrate, never narrow it), so a
    missing diff never weakens the existing label-based gate.
    """
    return _code_host_for(host_kind).fetch_pr_changed_paths(slug=slug, pr_id=pr_id)


def attach_touched_paths(clear: object, *, slug: str, pr_id: int, host_kind: str) -> None:
    """Populate ``clear.touched_paths`` from the forge's live changed-file list.

    Best-effort: a non-``MergeClear`` *clear* (the gate handles that refusal) or a
    forge error degrades to leaving ``touched_paths`` empty. The path detector can
    only WIDEN substrate over the recorded ``blast_class``, never narrow it, so a
    missing diff never weakens the existing label-based substrate gate.
    """
    if not isinstance(clear, MergeClear):
        return
    try:
        paths = fetch_pr_changed_paths(slug, pr_id, host_kind=host_kind)
    except Exception:  # noqa: BLE001 — a diff-fetch failure must never wedge the merge gate.
        logger.debug("ci_rollup: changed-paths fetch failed for %s#%s — substrate label stands", slug, pr_id)
        return
    clear.touched_paths = tuple(paths)


class _RollupEntry(TypedDict, total=False):
    """One ``gh ... statusCheckRollup`` entry — CheckRun or StatusContext."""

    conclusion: object
    status: object
    state: object
    name: object
    context: object
    startedAt: object
    completedAt: object
    createdAt: object


def _check_identity(entry: _RollupEntry) -> tuple[str, str]:
    """The dedupe key for one rollup entry: ``(typename, name)``.

    GitHub branch protection keys the newest check-run per check NAME within a
    namespace. A CheckRun's name is ``name``; a legacy StatusContext's name is
    ``context``. The ``__typename`` is part of the key so a CheckRun and a
    StatusContext that happen to share a name stay distinct identities (they are
    different check kinds the forge tracks separately).
    """
    typename = str(entry.get("__typename") or "")
    name = str(entry.get("name") or entry.get("context") or "")
    return (typename, name)


def _check_recency(entry: _RollupEntry) -> str:
    """The recency key for one rollup entry — newest wins on dedupe.

    CheckRun entries carry ISO-8601 ``completedAt`` / ``startedAt``; legacy
    StatusContext entries carry ``createdAt``. The lexicographic order of an
    ISO-8601 UTC timestamp is its chronological order, so plain string ``max``
    selects the newest entry. A missing timestamp sorts oldest (empty string),
    so a timestamped entry always supersedes an untimestamped one.
    """
    return str(entry.get("completedAt") or entry.get("startedAt") or entry.get("createdAt") or "")


def _dedupe_newest_per_name(rollup: "list[RawAPIDict]") -> "list[RawAPIDict]":
    """Reduce the rollup to the newest check-run per ``(typename, name)``.

    Matches GitHub branch-protection semantics: a cancelled/stale run that left
    a spurious FAILURE check-run on the head commit is superseded by a newer
    SUCCESS for the same name and must not block the merge. Entries with no
    identity (neither ``name`` nor ``context``) are kept as-is so a malformed
    rollup still classifies fail-closed via the existing per-entry path.
    """
    newest: dict[tuple[str, str], RawAPIDict] = {}
    unkeyed: list[RawAPIDict] = []
    for raw in rollup:
        if not isinstance(raw, dict):
            continue
        entry = cast("_RollupEntry", raw)
        identity = _check_identity(entry)
        if not identity[1]:
            unkeyed.append(dict(raw))
            continue
        incumbent = newest.get(identity)
        if incumbent is None or _check_recency(entry) >= _check_recency(cast("_RollupEntry", incumbent)):
            newest[identity] = dict(raw)
    return [*newest.values(), *unkeyed]


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


def _check_name(entry: object) -> str:
    """The NAME used to match a rollup entry against a required-status-check context.

    A CheckRun carries ``name``; a legacy StatusContext carries ``context``. The
    branch-protection required contexts are keyed by this name.
    """
    if not isinstance(entry, dict):
        return ""
    typed = cast("_RollupEntry", entry)
    return str(typed.get("name") or typed.get("context") or "")


def _required_verdicts_by_name(deduped: "list[RawAPIDict]", required_names: set[str]) -> dict[str, str]:
    """The worst deduped verdict per required context that actually reported.

    Keyed by name over ONLY *required_names*; a required context with no
    reporting check at all is absent from the map (the caller treats missing as
    pending). A non-required check (``eval``, advisory lanes) is dropped entirely
    — it can never influence the verdict. When several rollup entries share a
    required name the WORST verdict wins.
    """
    verdicts_by_name: dict[str, list[str]] = {}
    for check in deduped:
        name = _check_name(check)
        if name not in required_names:
            continue
        if verdict := _classify_check(check):
            verdicts_by_name.setdefault(name, []).append(verdict)
    return {name: _rollup_verdict(verdicts) for name, verdicts in verdicts_by_name.items()}


def _required_contexts_verdict(deduped: "list[RawAPIDict]", required_names: set[str]) -> str:
    """Verdict over ONLY the branch-protection-required contexts (§17.4.3 step 3).

    The authoritative required set is *required_names* (the repo's branch-
    protection ``required_status_checks`` contexts). Each required context must
    have a reporting check that is green; a required context that is failing →
    ``failed``, one still pending OR with no reporting check at all (missing) →
    ``pending`` (both refuse the merge, fail closed). When several rollup entries
    share a required name the WORST verdict wins.
    """
    reported = _required_verdicts_by_name(deduped, required_names)
    # A required context with no reporting check at all is "pending" (missing → refuse).
    return _rollup_verdict([reported.get(name, "pending") for name in required_names])


def classify_required_rollup(rollup: "list[RawAPIDict]", required_names: set[str]) -> str:
    """The SINGLE §17.4.3-step-3 verdict both the keystone and the PR-sweep route through.

    Green/pending/failed over ONLY the branch-protection-``required_names`` set,
    after deduping the rollup to the newest check-run per ``(typename, name)`` — a
    stale/cancelled FAILURE superseded by a newer SUCCESS for the same name does
    NOT block (parity with GitHub branch protection, which keys newest-per-context;
    the #2583/#2580 incident). An empty *required_names* means the base branch has
    no required-status-check gate → nothing to satisfy → ``green``.

    Both consumers — :func:`fetch_required_checks_status` (the keystone merge gate)
    and ``pr_sweep`` ``_ci_gate`` (the sweep pre-merge filter) — classify through
    this one function so the two can never re-diverge (the #12 sibling-classifier
    bug: the sweep hardcoded ``test (3.13)`` and blocked on non-required checks).
    """
    if not required_names:
        return "green"
    return _required_contexts_verdict(_dedupe_newest_per_name(rollup), required_names)


def failing_required_names(rollup: "list[RawAPIDict]", required_names: set[str]) -> set[str]:
    """The subset of *required_names* whose newest deduped check-run is failing.

    Present-and-failed only — a required context that is missing or still pending
    is NOT here. The PR-sweep reads this to tell its two special cases apart from a
    plain red: the uv-audit fallback (the ONLY failing required check is
    ``uv-audit``) and the repo-state remedy (every failing required check is a
    base-diffing repo-state check). Deduped identically to
    :func:`classify_required_rollup` so the failing set and the verdict agree.
    """
    reported = _required_verdicts_by_name(_dedupe_newest_per_name(rollup), required_names)
    return {name for name, verdict in reported.items() if verdict == "failed"}


def _required_context_names(backend: "CodeHostBackend", *, slug: str, pr_id: int) -> set[str] | None:
    """The branch-protection required context names, or ``None`` when indeterminate.

    ``None`` is the fail-CLOSED signal (the required-status-check endpoint could
    not be read); an EMPTY set is the determinate "no required gate configured".
    Both the keystone verdict and the sweep gate read the required set through
    this one extractor so they share the same fail-closed / no-gate semantics.
    """
    required = backend.fetch_required_status_check_contexts(slug=slug, pr_id=pr_id)
    if rollup_query_failed(required):
        return None
    return {str(entry["context"]) for entry in required if isinstance(entry, dict) and entry.get("context")}


def fetch_required_context_names(slug: str, pr_id: int, *, host_kind: str = "github") -> set[str] | None:
    """Live branch-protection required context names for the PR/MR base (the sweep's source).

    ``None`` when the required set is indeterminate (fail CLOSED); an EMPTY set
    when the base branch has no required-status-check gate (no gate → green). The
    same required set :func:`fetch_required_checks_status` scopes its keystone
    verdict to, exposed as a module-level function so ``pr_sweep`` reads the
    identical set instead of hardcoding ``test (3.13)`` (#12).
    """
    return _required_context_names(_code_host_for(host_kind), slug=slug, pr_id=pr_id)


def fetch_required_checks_status(slug: str, pr_id: int, *, host_kind: str = "github") -> str:
    """Live required-checks verdict for the PR/MR head — §17.4.3 step 3.

    Evaluated against the forge's live state at merge time (the authoritative
    set), NOT the ``gh_verify_result`` snapshot saved on the CLEAR. Returns
    ``"green"`` only when every branch-protection-REQUIRED context concluded
    successfully; ``"pending"`` while a required context is still running or has
    not reported; otherwise ``"failed"``.

    The backend returns the RAW rollup (GitHub ``statusCheckRollup`` entries,
    GitLab pipeline entries); core does the verdict classification here so the
    §17.4.3 ``green``/``pending``/``failed`` semantics stay in one place. A rollup
    query failure surfaces as the :data:`ROLLUP_QUERY_FAILED` sentinel → ``failed``.

    **GitHub — the required set is branch protection, not the whole rollup.** The
    ``statusCheckRollup`` reports EVERY check on the head commit, required or not
    (``eval``, advisory lanes, …). The authoritative required set is the repo's
    branch-protection ``required_status_checks`` contexts, fetched via
    :meth:`fetch_required_status_check_contexts`. Only a check whose name is in
    that set can block the merge; a non-required check NEVER blocks regardless of
    its conclusion (failed/pending/skipped). If the required set cannot be fetched
    the merge fails CLOSED (``failed``) — an indeterminate required set never
    falls open. An empty required set (the base branch has no required-status-
    check protection) means no gate → ``green``. The rollup is first deduped to
    the newest check-run per ``(typename, name)`` so a stale/cancelled FAILURE
    superseded by a newer SUCCESS for the same name does not false-block — parity
    with the forge's own branch protection, which keys newest-per-context.

    **GitLab** gates on the head pipeline's overall status (which aggregates the
    required jobs server-side); it needs the head SHA to pick the right
    (non-merge-train) pipeline, fetched via :func:`fetch_live_head_sha`.
    """
    backend = _code_host_for(host_kind)
    rollup = backend.fetch_required_checks_rollup(slug=slug, pr_id=pr_id)
    if rollup_query_failed(rollup):
        return "failed"
    if host_kind == "gitlab":
        return _gitlab_pipeline_verdict(backend, rollup, slug=slug, pr_id=pr_id)
    return _github_required_checks_verdict(backend, rollup, slug=slug, pr_id=pr_id)


def _gitlab_pipeline_verdict(
    backend: "CodeHostBackend",
    rollup: "list[RawAPIDict]",
    *,
    slug: str,
    pr_id: int,
) -> str:
    """GitLab §17.4.3 verdict: the head pipeline's overall status (aggregates required jobs)."""
    if not rollup:
        return "green"
    head_sha = backend.fetch_live_head_sha(slug=slug, pr_id=pr_id)
    head = _select_gitlab_head_pipeline(list(rollup), head_sha, slug=slug, pr_id=pr_id)
    if head is None:
        return "failed"
    return _classify_gitlab_pipeline(str(head.get("status") or ""))


def _github_required_checks_verdict(
    backend: "CodeHostBackend",
    rollup: "list[RawAPIDict]",
    *,
    slug: str,
    pr_id: int,
) -> str:
    """GitHub §17.4.3 verdict: scope the rollup to the branch-protection required contexts.

    Fail CLOSED when the required set is indeterminate; otherwise the shared
    :func:`classify_required_rollup` verdict over only the required contexts (an
    empty required set → ``green``, a non-required check never blocks).
    """
    required_names = _required_context_names(backend, slug=slug, pr_id=pr_id)
    if required_names is None:
        return "failed"  # fail CLOSED — the branch-protection required set is indeterminate
    return classify_required_rollup(rollup, required_names)


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
