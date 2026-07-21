"""Reap a worktree's per-compose-project docker containers and images.

Two boundaries keep a reap inside teatree's own footprint.

*Which artifacts* — Docker Compose stamps every container *and* every image it
builds for a project with the ``com.docker.compose.project=<project>`` label;
filtering by it reaps exactly the artifacts a stack created and nothing else.

*Which projects* — the label alone does not say whose stack it is, so
enumerating every labelled project and subtracting the ones still live treats
the deploy stack and unrelated user projects as orphans. Candidate selection
therefore also requires teatree's own naming scheme
(:func:`is_worktree_compose_project`), applied in the single seam
:func:`_reapable_candidates` that both reaping flavours share.

Base / official images
(``postgres``, ``python``, ``node``, ``redis``, ``nginx``) and the single
master ``{image_name}:base`` image are built via ``docker build`` (see
:mod:`teatree.docker.build`), not compose, so they carry no compose-project
label and are never enumerated under a removed worktree's project.

This is a core utility — the generic engine (BLUEPRINT §6.0) — that the
docker-using overlay reaches through
``OverlayProvisioning.reap_external_resources``. Tolerant of an unavailable
docker binary (CI sandboxes, hermetic tests) so cleanup paths funnelling
through it never break when there is no daemon to talk to.
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

logger = logging.getLogger(__name__)

_PROJECT_LABEL = "com.docker.compose.project"
_LIST_TIMEOUT = 30
_REMOVE_TIMEOUT = 60

# Teatree's own compose-project naming scheme (``<repo_path>-wt<ticket.pk>``,
# minted by ``Worktree._build_compose_project``) — the ownership marker that
# keeps foreign stacks off every reap candidate list.
_WORKTREE_PROJECT_RE = re.compile(r"-wt\d+$")


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


def _container_project_labels() -> list[str]:
    """Project label values across all compose-built containers.

    The container formatter context exposes ``.Label "<key>"``, so the project
    name is read directly from one ``docker ps`` call.
    """
    label_filter = ["--filter", f"label={_PROJECT_LABEL}"]
    label_format = ["--format", f'{{{{.Label "{_PROJECT_LABEL}"}}}}']
    return _docker_lines(["docker", "ps", "-a", *label_filter, *label_format], timeout=_LIST_TIMEOUT)


def _image_project_labels() -> list[str]:
    """Project label values across all compose-built images.

    The image formatter context has NO ``.Label`` field (only ``.Labels``, a
    flattened string), so ``docker images --format '{{.Label "<key>"}}'`` raises
    ``template parsing error: can't evaluate field Label in type
    *formatter.imageContext`` and the image reap silently fails (#2361). Read the
    label-filtered image ids first, then resolve each id's project label via
    ``docker image inspect`` whose ``.Config.Labels`` map IS indexable.
    """
    ids = _docker_lines(
        ["docker", "images", "--filter", f"label={_PROJECT_LABEL}", "--format", "{{.ID}}"],
        timeout=_LIST_TIMEOUT,
    )
    if not ids:
        return []
    return _docker_lines(
        ["docker", "image", "inspect", "--format", f'{{{{index .Config.Labels "{_PROJECT_LABEL}"}}}}', *ids],
        timeout=_LIST_TIMEOUT,
    )


def list_compose_projects() -> set[str]:
    """Every compose project on the host — UNFILTERED, including foreign ones.

    Raw enumeration for diagnostics. NEVER feed this to a reaper: it sees the
    deploy stack and unrelated user projects too. Destructive callers go
    through :func:`_reapable_candidates`.
    """
    return {name for name in (*_container_project_labels(), *_image_project_labels()) if name}


def is_worktree_compose_project(project: str) -> bool:
    """Whether *project* is named the way teatree provisions worktree stacks.

    ``Worktree._build_compose_project`` mints ``<repo_path>-wt<ticket.pk>`` and
    freezes it on the row, and ``Worktree.ticket`` is non-nullable, so every
    stack teatree has ever provisioned matches. Being a name test rather than a
    label test, it also covers stacks provisioned before this gate existed --
    an ownership label could not see those.

    A foreign project coincidentally named ``*-wt<digits>`` would still pass;
    that residual is accepted because the alternative (deriving from live rows)
    cannot work for the orphan case, whose whole premise is that the row is
    already gone.
    """
    return bool(_WORKTREE_PROJECT_RE.search(project))


def _reapable_candidates(live_projects: set[str]) -> list[str]:
    """Reap candidates: teatree-provisioned projects that are no longer live.

    The single seam both reaping flavours share, so ownership cannot be
    bypassed by adding a caller. Previously each selector computed
    ``list_compose_projects() - live_projects`` -- "everything on the host
    minus what I can currently prove is mine" -- which made every foreign
    stack an orphan and tore down the deploy stack and an unrelated user
    project. The set must be "mine, minus what is still live".
    """
    return sorted(p for p in list_compose_projects() - live_projects if is_worktree_compose_project(p))


def reap_orphan_compose_projects(live_projects: set[str]) -> list[ReapResult]:
    results: list[ReapResult] = []
    for project in _reapable_candidates(live_projects):
        result = reap_compose_project(project)
        if not result.is_noop:
            results.append(result)
    return results


# ── Stale-stack reaping (age-keyed orphan teardown, #2207) ──────────────────
#
# A teatree-provisioned stack with NO live worktree row is not necessarily safe
# to tear down at any moment: a parallel session may have hand-started it
# minutes ago inside a worktree (a `docker compose -f docker-compose.test.yml`
# run mid-flight, inheriting the worktree's COMPOSE_PROJECT_NAME). The stale
# reaper therefore keys on AGE: such a project is reaped only when its
# newest container lifecycle event (created / started / finished) is older
# than the threshold. Anything younger — or whose age cannot be determined —
# is KEPT (fail-safe). This makes the orphan reap safe to invoke automatically
# on provision/start, instead of only inside an explicit `clean-all`, so
# abandoned stacks stop squatting host CPU/RAM for 8+ hours.

# Docker reports a zero Time for "never happened" (e.g. FinishedAt of a
# running container).
_DOCKER_ZERO_TIME_PREFIX = "0001-01-01"


def _parse_docker_timestamp(raw: str) -> "datetime | None":
    """Parse one docker RFC3339 timestamp (nanosecond precision) to aware UTC.

    Returns ``None`` for the docker zero value, an empty field, or an
    unparsable string — the caller treats unknown as "cannot confirm stale"
    and keeps the stack.
    """
    value = raw.strip()
    if not value or value.startswith(_DOCKER_ZERO_TIME_PREFIX):
        return None
    # datetime.fromisoformat caps fractional seconds at 6 digits; docker emits 9.
    value = re.sub(r"\.(\d{6})\d+", r".\1", value)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def project_last_activity(project: str) -> "datetime | None":
    """The newest container lifecycle timestamp of *project*, or ``None``.

    ``None`` means "could not determine" (no containers carry the label, or
    docker is unavailable) — the caller must fail safe and keep the stack.
    """
    ids = _list_project_containers(project)
    if not ids:
        return None
    lines = _docker_lines(
        [
            "docker",
            "inspect",
            "--format",
            "{{.Created}}|{{.State.StartedAt}}|{{.State.FinishedAt}}",
            *ids,
        ],
        timeout=_LIST_TIMEOUT,
    )
    stamps = [
        parsed for line in lines for field in line.split("|") if (parsed := _parse_docker_timestamp(field)) is not None
    ]
    return max(stamps) if stamps else None


def stale_compose_projects(
    live_projects: set[str],
    *,
    min_age_minutes: int,
    now: "datetime | None" = None,
) -> list[str]:
    """The teatree-provisioned compose projects whose newest activity is older than the threshold.

    Pure selection (no teardown) so callers can dry-run. Fail-safe: a project
    that teatree did not provision (:func:`is_worktree_compose_project`), whose
    age cannot be determined, or with any activity younger than
    ``min_age_minutes``, is never selected.
    """
    moment = now or datetime.now(tz=UTC)
    cutoff = moment - timedelta(minutes=min_age_minutes)
    stale: list[str] = []
    for project in _reapable_candidates(live_projects):
        last_activity = project_last_activity(project)
        if last_activity is None:
            logger.info("stale-stack reaper: keeping %r (age unknown — fail-safe)", project)
            continue
        if last_activity > cutoff:
            logger.info("stale-stack reaper: keeping %r (active %s, younger than threshold)", project, last_activity)
            continue
        stale.append(project)
    return stale


def reap_stale_compose_projects(
    live_projects: set[str],
    *,
    min_age_minutes: int,
    now: "datetime | None" = None,
) -> list[ReapResult]:
    """Tear down the stale unowned compose projects (see :func:`stale_compose_projects`)."""
    results: list[ReapResult] = []
    for project in stale_compose_projects(live_projects, min_age_minutes=min_age_minutes, now=now):
        result = reap_compose_project(project)
        if not result.is_noop:
            results.append(result)
    return results
