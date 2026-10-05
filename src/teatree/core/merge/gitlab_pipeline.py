"""GitLab pipeline verdict classification — the §17.4.3 GitLab half of the CI rollup.

Split out of ``ci_rollup`` (module-health extraction, #4844) so the GitLab-specific
pipeline-picking/classification concern lives on its own: the head-pipeline selector
skips merge-train pipelines and matches on the MR head SHA, and
``classify_gitlab_pipeline`` maps GitLab's pipeline statuses onto the shared
green/pending/failed vocabulary plus GitLab's terminal ``skipped``, which the verdict
resolves from the project's own skipped-pipeline setting.
``ci_rollup.CodeHostQuery.required_checks_status`` is the sole caller of
:func:`_gitlab_pipeline_verdict`, re-exported from there.
"""

import logging
from typing import TYPE_CHECKING, TypedDict, cast

from teatree.core.modelkit.forge_readability import CHECKS_FAILED, CHECKS_UNREADABLE, LiveHeadRead

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend
    from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)


def _gitlab_pipeline_verdict(
    backend: "CodeHostBackend",
    rollup: "list[RawAPIDict]",
    *,
    slug: str,
    pr_id: int,
) -> str:
    """GitLab §17.4.3 verdict: the head pipeline's overall status (aggregates required jobs)."""
    if not rollup:
        # No pipeline has run at all — that is NOT proof the required jobs passed
        # (a project could have CI disabled, or the head pipeline is not created
        # yet). Fail closed to ``pending`` so an empty pipeline list never merges as
        # "all checks passed"; a genuinely CI-less project is unblocked by the same
        # required-context floor the GitHub path uses.
        return "pending"
    # Normalised, never raw: an UNREADABLE head must reach the pipeline picker as
    # the same empty string an unnamed head does, not as a sentinel oid to match on.
    head_sha = LiveHeadRead.of(backend.fetch_live_head_sha(slug=slug, pr_id=pr_id)).sha
    head = _select_gitlab_head_pipeline(list(rollup), head_sha, slug=slug, pr_id=pr_id)
    if head is None:
        return "failed"
    verdict = classify_gitlab_pipeline(str(head.get("status") or ""))
    return _skipped_pipeline_verdict(backend, slug=slug) if verdict == _GITLAB_PIPELINE_SKIPPED else verdict


def _skipped_pipeline_verdict(backend: "CodeHostBackend", *, slug: str) -> str:
    """GitLab's own rule: a skipped head merges only where the project counts skipped pipelines as successful."""
    try:
        allowed = backend.get_repo(repo=slug).get("allow_merge_on_skipped_pipeline")
    except Exception:  # noqa: BLE001 — an unread project setting refuses the merge; it must never crash the gate.
        logger.warning("merge_execution: could not read %s's skipped-pipeline setting — failing closed", slug)
        return CHECKS_UNREADABLE
    if allowed is True:
        return "green"
    if allowed is False:
        logger.info("merge_execution: %s's head pipeline was skipped and the project requires success", slug)
        return CHECKS_FAILED
    logger.warning("merge_execution: %s's skipped-pipeline setting is absent — failing closed", slug)
    return CHECKS_UNREADABLE


_GITLAB_PIPELINE_SKIPPED = "skipped"
_GITLAB_PIPELINE_GREEN_STATUSES = frozenset({"success"})
_GITLAB_PIPELINE_PENDING_STATUSES = frozenset(
    {"pending", "running", "preparing", "scheduled", "waiting_for_resource", "created", "manual"},
)


def classify_gitlab_pipeline(status: str) -> str:
    """Map a GitLab pipeline status string to ``green`` / ``pending`` / ``skipped`` / ``failed``.

    GitLab pipeline statuses (per the REST API documentation): ``created``,
    ``waiting_for_resource``, ``preparing``, ``pending``, ``running``,
    ``success``, ``failed``, ``canceled``, ``skipped``, ``manual``,
    ``scheduled``. ONLY ``success`` is green.

    ``manual`` is pending: it is blocked on a manual gate whose later (required)
    stages have not run, and classifying it green merged a keystone MR on
    not-passed CI (the §17.4.3 fail-toward-green hole). ``skipped`` (every job
    excluded by ``rules:``) is terminal but never green, so it is its own class:
    nothing will ever run, and pending would hold it forever. ``failed`` /
    ``canceled`` are failed; everything else is pending.
    """
    s = status.lower()
    if s in _GITLAB_PIPELINE_GREEN_STATUSES:
        return "green"
    if s == _GITLAB_PIPELINE_SKIPPED:
        return _GITLAB_PIPELINE_SKIPPED
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
