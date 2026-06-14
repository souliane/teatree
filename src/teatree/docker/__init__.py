"""Docker helpers — base-image sharing, compose orchestration."""

from teatree.docker.build import ensure_base_image
from teatree.docker.reap import ReapResult, list_compose_projects, reap_compose_project, reap_orphan_compose_projects
from teatree.docker.reclaim import PruneOutcome, ReclaimReport, ReclaimStep, reclaim_disk

__all__ = [
    "PruneOutcome",
    "ReapResult",
    "ReclaimReport",
    "ReclaimStep",
    "ensure_base_image",
    "list_compose_projects",
    "reap_compose_project",
    "reap_orphan_compose_projects",
    "reclaim_disk",
]
