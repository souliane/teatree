"""Reap a worktree's per-compose-project docker containers and images.

Docker Compose stamps every container *and* every image it builds for a
project with the ``com.docker.compose.project=<project>`` label. That single
label is the safety boundary: filtering by it reaps exactly the artifacts a
worktree's stack created and nothing else. Base / official images
(``postgres``, ``python``, ``node``, ``redis``, ``nginx``) and the main-clone's
shared ``{image_name}:deps-<hash>`` image are built via ``docker build`` (see
:mod:`teatree.docker.build`), not compose, so they carry no compose-project
label and are never enumerated under a removed worktree's project.

This is a core utility — the generic engine (BLUEPRINT §6.0) — that the
docker-using overlay reaches through
``OverlayBase.reap_worktree_external_resources``. Tolerant of an unavailable
docker binary (CI sandboxes, hermetic tests) so cleanup paths funnelling
through it never break when there is no daemon to talk to.
"""

import logging
from dataclasses import dataclass

from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

logger = logging.getLogger(__name__)

_PROJECT_LABEL = "com.docker.compose.project"
_LIST_TIMEOUT = 30
_REMOVE_TIMEOUT = 60


@dataclass(frozen=True, slots=True)
class ReapResult:
    project: str
    containers_removed: int = 0
    images_removed: int = 0

    @property
    def is_noop(self) -> bool:
        return self.containers_removed == 0 and self.images_removed == 0

    def __str__(self) -> str:
        return (
            f"Reaped docker project {self.project}: "
            f"{self.containers_removed} container(s), {self.images_removed} image(s)"
        )


def _docker_lines(cmd: list[str], *, timeout: int) -> list[str]:
    try:
        result = run_allowed_to_fail(cmd, expected_codes=None, timeout=timeout)
    except (FileNotFoundError, PermissionError) as exc:
        logger.debug("docker unavailable, skipping %s: %s", cmd[:3], exc)
        return []
    except TimeoutExpired:
        logger.warning("docker command timed out: %s", cmd[:3])
        return []
    if result.returncode != 0:
        logger.warning("docker %s failed: %s", cmd[:3], result.stderr.strip()[:300])
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _project_filter(project: str) -> list[str]:
    return ["--filter", f"label={_PROJECT_LABEL}={project}"]


def _list_project_containers(project: str) -> list[str]:
    return _docker_lines(
        ["docker", "ps", "-a", *_project_filter(project), "--format", "{{.ID}}"],
        timeout=_LIST_TIMEOUT,
    )


def _list_project_images(project: str) -> list[str]:
    return _docker_lines(
        ["docker", "images", *_project_filter(project), "--format", "{{.ID}}"],
        timeout=_LIST_TIMEOUT,
    )


def _remove(kind: str, ids: list[str]) -> int:
    if not ids:
        return 0
    flag = "rm" if kind == "container" else "rmi"
    removed = _docker_lines(["docker", flag, "-f", *ids], timeout=_REMOVE_TIMEOUT)
    return len(removed) or len(ids)


def reap_compose_project(project: str) -> ReapResult:
    if not project:
        return ReapResult(project=project)
    containers = _remove("container", _list_project_containers(project))
    images = _remove("image", _list_project_images(project))
    if containers or images:
        logger.info("reaped docker project %s: %s containers, %s images", project, containers, images)
    return ReapResult(project=project, containers_removed=containers, images_removed=images)


def list_compose_projects() -> set[str]:
    label_filter = ["--filter", f"label={_PROJECT_LABEL}"]
    label_format = ["--format", f'{{{{.Label "{_PROJECT_LABEL}"}}}}']
    containers = _docker_lines(["docker", "ps", "-a", *label_filter, *label_format], timeout=_LIST_TIMEOUT)
    images = _docker_lines(["docker", "images", *label_filter, *label_format], timeout=_LIST_TIMEOUT)
    return {name for name in (*containers, *images) if name}


def reap_orphan_compose_projects(live_projects: set[str]) -> list[ReapResult]:
    orphans = sorted(list_compose_projects() - live_projects)
    results: list[ReapResult] = []
    for project in orphans:
        result = reap_compose_project(project)
        if not result.is_noop:
            results.append(result)
    return results
