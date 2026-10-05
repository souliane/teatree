"""Which GitLab pipeline speaks for a merge request's head commit — shared by the merge gate and the MR listing."""

import logging
from typing import TypedDict, cast

logger = logging.getLogger(__name__)


class GitlabPipeline(TypedDict, total=False):
    """One entry of GitLab's ``merge_requests/<iid>/pipelines`` list."""

    id: object
    sha: object
    ref: object
    source: object
    status: object
    web_url: object
    allow_merge_on_skipped_pipeline: object


def _is_merge_train_pipeline(pipeline: GitlabPipeline) -> bool:
    ref = str(pipeline.get("ref") or "")
    source = str(pipeline.get("source") or "")
    return source == "merge_train" or "/train" in ref


def select_head_pipeline(
    pipelines: list[object],
    head_sha: str,
    *,
    slug: str,
    pr_id: int,
) -> GitlabPipeline | None:
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
    entries = [cast("GitlabPipeline", p) for p in pipelines if isinstance(p, dict)]
    candidates = [e for e in entries if not _is_merge_train_pipeline(e)]
    if head_sha:
        for pipeline in candidates:
            if str(pipeline.get("sha") or "") == head_sha:
                return pipeline
        logger.info(
            "GitLab head pipeline: none matches MR head %s for %s#%s (non-train candidates: %s) — failing closed",
            head_sha,
            slug,
            pr_id,
            [str(p.get("sha") or "") for p in candidates],
        )
        return None
    logger.info(
        "GitLab head pipeline: MR head SHA unavailable for %s#%s — falling back to newest non-train pipeline",
        slug,
        pr_id,
    )
    return candidates[0] if candidates else None
