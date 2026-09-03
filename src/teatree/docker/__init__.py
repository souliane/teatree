"""Docker helpers — base-image sharing, compose orchestration."""

from teatree.docker.build import ensure_base_image
from teatree.docker.reap import ReapResult, list_compose_projects, reap_compose_project, reap_orphan_compose_projects
from teatree.docker.reclaim import PruneOutcome, ReclaimReport, ReclaimStep, reclaim_disk
from teatree.docker.venue import DockerVenue, docker_venue

__all__ = [
    "DockerVenue",
    "PruneOutcome",
    "ReapResult",
    "ReclaimReport",
    "ReclaimStep",
    "docker_venue",
    "ensure_base_image",
    "list_compose_projects",
    "reap_compose_project",
    "reap_orphan_compose_projects",
    "reclaim_disk",
]
