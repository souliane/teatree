"""Reap a worktree's per-compose-project docker containers and images.

Two boundaries keep a reap inside teatree's own footprint.

*Which artifacts* — Docker Compose stamps every container *and* every image it
builds for a project with the ``com.docker.compose.project=<project>`` label;
filtering by it reaps exactly the artifacts a stack created and nothing else.

*Which projects* — the label alone does not say whose stack it is, so
enumerating every labelled project and tearing down the settled ones treats the
deploy stack and unrelated user projects as orphans. Candidate selection
therefore requires a proof of ownership: teatree's own ``-wt<pk>`` naming scheme
(:func:`is_worktree_compose_project`), or the caller's registry-derived set for
the names that scheme cannot mint.

*Which of them are safe to touch* — a project with a container still up is one
somebody is using, whatever its age: a container that never stopped carries a
days-old ``StartedAt`` and no ``FinishedAt`` at all, so the age test alone reads
a live stack as abandoned. Both proofs are applied in the single seam
:func:`_reapable_candidates` that both reaping flavours share, and a docker that
could not answer reaps nothing.

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
from pathlib import PurePosixPath

from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

logger = logging.getLogger(__name__)

_PROJECT_LABEL = "com.docker.compose.project"
# Compose stamps the directory it was invoked from on every container. It is the ONLY
# per-container evidence of WHICH checkout a project belongs to, and the name alone
# cannot say: measured on one host, a single `<basename>-test` project spanned two
# unrelated checkouts, neither of them a registered worktree.
_WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"
# Which compose file(s) the stack was brought up from, comma-separated and ordered
# as compose received them. It is the only label that distinguishes a repo's own
# throwaway test harness from the dev or deploy stack of the very same checkout.
_CONFIG_FILES_LABEL = "com.docker.compose.project.config_files"
_LIST_TIMEOUT = 30
_REMOVE_TIMEOUT = 60

# The suffix a repo's own test harness appends to its checkout directory name
# when it brings a stack up beside the worktree's, minting ``<checkout-dir>-test``.
# Compose gives that same name to ANY directory called ``<something>-test``, so it
# identifies the harness and never the owner — the registry does that.
TEST_STACK_SUFFIX = "-test"

# The compose file a repo's own test harness brings its stack up from. Every file in
# the label must be this one: a stack sharing a checkout with the test harness but
# built from any other file is a dev stack, and the deploy stack's own label carries
# `deploy/docker-compose.yml` plus a /tmp override — so this test refuses it on the
# file name alone, before its checkout or its liveness is ever consulted.
_TEST_HARNESS_COMPOSE_FILE = "docker-compose.test.yml"

# Teatree's own compose-project naming scheme (``<repo_path>-wt<ticket.pk>``,
# minted by ``Worktree._build_compose_project``) — the one name that proves
# ownership on its own, and keeps foreign stacks off every reap candidate list.
_WORKTREE_PROJECT_RE = re.compile(r"-wt\d+$")

# `paused` and `restarting` are as alive as `running` — only a project whose
# every container has settled is idle.
_LIVE_CONTAINER_STATES = frozenset({"running", "restarting", "paused"})


@dataclass(frozen=True, slots=True)
class OwnedStacks:
    """What a caller can PROVE it owns — project names, and the checkouts they run from.

    Both halves are needed because the two admission routes carry different weight.
    A ``-wt<pk>`` name is self-identifying (teatree minted it), so the name is the whole
    proof. Every OTHER name in *project_names* — the ``<checkout-dir>-test`` sibling a
    repo's own harness brings up — is a name compose derives from a directory basename,
    so it proves nothing on its own: two unrelated checkouts sharing a basename share
    the project. Those are admitted only when every container's working_dir is a
    checkout in *checkout_paths*. All three default empty, so a caller that supplies none
    reaps only teatree's own self-identifying stacks.

    *checkout_roots* exists because the other two cannot reach the ORPHAN case. Both
    derive ownership from a live ``Worktree`` row, and the defining property of a leaked
    stack is that its row is gone — so a ``<checkout>-test`` sibling outlived its row was
    unreachable by every reaper at any age, which is how nine settled stacks accumulated
    on one host. A root is teatree's own provisioning territory, so a test-harness stack
    running from inside one is teatree's whether or not a row still says so. The roots
    travel as STRINGS the caller resolved, and are matched against the container label
    without touching the filesystem: a checkout that has been deleted is precisely the
    case this route serves, and a path visible in one execution context is routinely
    unreadable in another.
    """

    project_names: frozenset[str] = frozenset()
    checkout_paths: frozenset[str] = frozenset()
    checkout_roots: frozenset[str] = frozenset()


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


def _docker_output(cmd: list[str], *, timeout: int) -> list[str] | None:
    """Stripped stdout lines, or ``None`` when docker refused, timed out, or was absent.

    A refusal and an empty success are different answers: collapsing them lets a failed
    removal be counted as a completed one.
    """
    try:
        result = run_allowed_to_fail(cmd, expected_codes=None, timeout=timeout)
    except (FileNotFoundError, PermissionError) as exc:
        logger.debug("docker unavailable, skipping %s: %s", cmd[:3], exc)
        return None
    except TimeoutExpired:
        logger.warning("docker command timed out: %s", cmd[:3])
        return None
    if result.returncode != 0:
        logger.warning("docker %s failed: %s", cmd[:3], result.stderr.strip()[:300])
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _docker_lines(cmd: list[str], *, timeout: int) -> list[str]:
    return _docker_output(cmd, timeout=timeout) or []


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
    removed = _docker_output(["docker", flag, "-f", *ids], timeout=_REMOVE_TIMEOUT)
    if removed is None:
        return 0
    # `docker rmi` prints Untagged:/Deleted: lines rather than one id per removal, so a
    # succeeding call with an unmatched line count still removed everything asked for.
    return len(removed) or len(ids)


def reap_compose_project(project: str) -> ReapResult:
    if not project:
        return ReapResult(project=project)
    containers = _remove("container", _list_project_containers(project))
    images = _remove("image", _list_project_images(project))
    if containers or images:
        logger.info("reaped docker project %s: %s containers, %s images", project, containers, images)
    return ReapResult(project=project, containers_removed=containers, images_removed=images)


def _container_project_states() -> list[tuple[str, str]] | None:
    """``(project, state)`` per compose-built container, or ``None`` when docker refused.

    The container formatter context exposes both ``.Label "<key>"`` and
    ``.State``, so one ``docker ps`` answers both "which projects exist" and
    "which of them are doing anything". The refusal is propagated rather than
    flattened to an empty list because a reaper reading it as "nothing is
    running" would tear down every stack on the host.
    """
    label_filter = ["--filter", f"label={_PROJECT_LABEL}"]
    label_format = ["--format", f'{{{{.Label "{_PROJECT_LABEL}"}}}}|{{{{.State}}}}']
    lines = _docker_output(["docker", "ps", "-a", *label_filter, *label_format], timeout=_LIST_TIMEOUT)
    if lines is None:
        return None
    pairs = (line.partition("|") for line in lines)
    return [(project, state) for project, _, state in pairs if project]


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
    containers = (project for project, _ in _container_project_states() or [])
    return {name for name in (*containers, *_image_project_labels()) if name}


def running_compose_projects() -> set[str] | None:
    """Projects with at least one container still doing something, or ``None``.

    ``None`` means docker could not answer, which is NOT an empty live set — the
    caller must reap nothing rather than treat every stack as settled.
    """
    states = _container_project_states()
    if states is None:
        return None
    return {project for project, state in states if state in _LIVE_CONTAINER_STATES}


def is_worktree_compose_project(project: str) -> bool:
    """Whether *project* carries the name teatree itself mints for a worktree stack.

    ``Worktree._build_compose_project`` mints ``<repo_path>-wt<ticket.pk>`` and
    freezes it on the row, and ``Worktree.ticket`` is non-nullable, so every stack
    teatree has ever provisioned matches. Being a name test rather than a label
    test, it also covers stacks provisioned before this gate existed -- an
    ownership label could not see those.

    A foreign project coincidentally named ``*-wt<digits>`` would still pass; that
    residual is accepted because the alternative (deriving from live rows) cannot
    work for the orphan case, whose whole premise is that the row is already gone.
    The ``<checkout-dir>-test`` sibling a repo's test harness brings up is NOT
    admitted here: compose gives that name to any directory called the same thing,
    so it is proved by the caller's registry-derived set instead.
    """
    return bool(_WORKTREE_PROJECT_RE.search(project))


def _project_label_values(project: str, label: str) -> set[str] | None:
    """Every value of *label* across *project*'s containers, or ``None`` to keep it.

    ``None`` on anything short of a complete answer: docker refusing, the project
    carrying no containers, or ANY container carrying no such label. Every caller's
    rule is ALL containers, so a container it cannot place is a container it cannot
    own, and the whole project is kept.

    The container ID leads each row because it is the one field always present. Asked
    for the label alone, an unlabelled container renders an EMPTY line, and empty lines
    are dropped on the way back — so a project with three containers and one unlabelled
    answered with two owned paths, matched cleanly, and was torn down WITH the container
    nothing had vouched for. Fail-open in a destructive path, invisible in the output.
    """
    lines = _docker_output(
        [
            "docker",
            "ps",
            "-a",
            *_project_filter(project),
            "--format",
            f'{{{{.ID}}}}\t{{{{.Label "{label}"}}}}',
        ],
        timeout=_LIST_TIMEOUT,
    )
    if not lines:
        return None
    values: set[str] = set()
    for line in lines:
        _container_id, _, value = line.partition("\t")
        if not value.strip():
            return None
        values.add(value.strip())
    return values or None


def _project_working_dirs(project: str) -> set[str] | None:
    """Every checkout *project*'s containers were brought up from, or ``None`` to keep it."""
    return _project_label_values(project, _WORKING_DIR_LABEL)


def _admitted_by_path(project: str, checkout_paths: frozenset[str]) -> bool:
    """Whether every container of *project* runs from a checkout the caller owns.

    ALL of them, not any: a reap is per project LABEL, so tearing the project down on
    one matching container would take a foreign checkout's containers with it.
    """
    working_dirs = _project_working_dirs(project)
    if working_dirs is None:
        logger.info("reaper: keeping %r (cannot read its working dirs — fail-safe)", project)
        return False
    foreign = working_dirs - checkout_paths
    if foreign:
        logger.info("reaper: keeping %r (runs from checkouts we do not own: %s)", project, sorted(foreign))
        return False
    return True


def sibling_test_project(checkout_path: str) -> str:
    """The project a repo's own test harness mints beside a checkout, or ``""``.

    Compose derives an unnamed project from the directory it runs in, so a harness
    invoked inside the checkout gets ``<checkout-dir>-test``. That name is a
    prediction about another tool's behaviour, and three call sites were each
    re-deriving it -- the reaper's ownership set, the overlay's teardown steps, and
    the stop path -- so it is spelled once here.

    ``""`` for an empty path: without a directory there is no name to predict, and
    guessing one aims a teardown at a project nothing ever started.
    """
    if not checkout_path:
        return ""
    return f"{PurePosixPath(checkout_path).name}{TEST_STACK_SUFFIX}"


def _built_by_the_test_harness(project: str) -> bool:
    """Whether EVERY compose file *project* was brought up from is the test harness's.

    All of them, not the first: an extra file can redefine any service, so a stack
    assembled from the test harness plus something else is not the harness's stack.
    The label is comma-separated, and the deploy stack's real value carries a second,
    ``/tmp`` override — which a suffix test on the raw label would read as a non-match
    for the right answer by luck, and would read as a MATCH the day the override sorts
    last. Split it and judge every entry.
    """
    labels = _project_label_values(project, _CONFIG_FILES_LABEL)
    if labels is None:
        logger.info("reaper: keeping %r (cannot read its compose files — fail-safe)", project)
        return False
    files = (entry.strip() for label in labels for entry in label.split(","))
    return all(path.rsplit("/", 1)[-1] == _TEST_HARNESS_COMPOSE_FILE for path in files if path)


def _under_a_known_root(path: str, roots: frozenset[str]) -> bool:
    """Whether *path* lies inside one of *roots* — a pure string containment test.

    Deliberately never stats the filesystem. The checkout this asks about is usually
    GONE (that is the whole orphan case), and a path readable in one execution context
    is routinely absent in another, so a probe would answer "not ours" for both the
    leaked stack it must reap and the live one it must keep. Comparing on separators
    keeps ``/w/repo-old`` out of ``/w/repo``.
    """
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


def _is_orphaned_test_stack(project: str, roots: frozenset[str]) -> bool:
    """Whether *project* is a leaked ``<checkout>-test`` stack teatree's own roots own.

    Ownership on PATH evidence, for the case no live row can speak to. Four proofs,
    each of which alone refuses the deploy stack: the name carries the suffix compose
    mints for a test sibling; every container ran from one checkout under a root
    teatree provisions into; the name is the one THAT directory mints, so a project
    merely passing through an owned checkout cannot borrow its ownership; and every
    compose file is the repo's own test harness.
    """
    if not roots or not project.endswith(TEST_STACK_SUFFIX):
        return False
    working_dirs = _project_working_dirs(project)
    if working_dirs is None or len(working_dirs) != 1:
        return False
    checkout = next(iter(working_dirs))
    if not _under_a_known_root(checkout, roots):
        return False
    if project != f"{checkout.rstrip('/').rsplit('/', 1)[-1]}{TEST_STACK_SUFFIX}":
        logger.info("reaper: keeping %r (its name is not the one %s mints)", project, checkout)
        return False
    return _built_by_the_test_harness(project)


def _reapable_candidates(owned: OwnedStacks) -> list[str]:
    """Reap candidates: projects teatree owns with nothing still running in them.

    The single seam both reaping flavours share, so neither proof can be bypassed
    by adding a caller. Ownership has two routes of unequal weight: the
    self-identifying ``-wt<pk>`` name teatree mints, which proves itself, and a name
    from the caller's registry, which does NOT — compose derives it from a directory
    basename, so it is admitted only once every container's working_dir is a checkout
    the caller owns. Anything else on the host — the deploy stack, an unrelated user
    project, a same-basename ``-test`` stack of a checkout nobody registered — is never
    a candidate. Liveness is the container state, because the age test both flavours
    build on cannot see it: a container that never stopped carries a days-old
    ``StartedAt`` and the docker zero ``FinishedAt``, so it reads as abandoned while it
    serves traffic.
    """
    running = running_compose_projects()
    if running is None:
        logger.warning("docker could not report container states; reaping no compose project this pass")
        return []
    mine = {
        project
        for project in list_compose_projects()
        if is_worktree_compose_project(project)
        or (project in owned.project_names and _admitted_by_path(project, owned.checkout_paths))
        or _is_orphaned_test_stack(project, owned.checkout_roots)
    }
    return sorted(mine - running)


def orphan_compose_projects(owned: OwnedStacks) -> list[str]:
    """The projects :func:`reap_orphan_compose_projects` would tear down — selection only.

    Public so ``clean-all --dry-run`` previews this pass through the same ownership
    seam a live run uses, rather than describing it from the outside (#3489).
    """
    return _reapable_candidates(owned)


def reap_orphan_compose_projects(owned: OwnedStacks) -> list[ReapResult]:
    results: list[ReapResult] = []
    for project in _reapable_candidates(owned):
        result = reap_compose_project(project)
        if not result.is_noop:
            results.append(result)
    return results


# ── Stale-stack reaping (age-keyed orphan teardown, #2207) ──────────────────
#
# A teatree-owned stack that is not running is not necessarily safe to tear down
# at any moment: a parallel session may have hand-started and stopped it minutes
# ago inside a worktree (a `docker compose -f docker-compose.test.yml` run
# mid-flight, inheriting the worktree's COMPOSE_PROJECT_NAME). The stale reaper
# therefore adds AGE on top of the shared seam's ownership + liveness proofs:
# such a project is reaped only when its newest container lifecycle event
# (created / started / finished) is older than the threshold. Anything younger —
# or whose age cannot be determined — is KEPT (fail-safe). This makes the orphan
# reap safe to invoke automatically on provision/start, instead of only inside an
# explicit `clean-all`, so abandoned stacks stop squatting host CPU/RAM for 8+ hours.

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
    owned: OwnedStacks,
    *,
    min_age_minutes: int,
    now: "datetime | None" = None,
) -> list[str]:
    """The teatree-owned compose projects whose newest activity is older than the threshold.

    Pure selection (no teardown) so callers can dry-run. Fail-safe: a project
    teatree cannot prove it owns, one still running, one whose age cannot be
    determined, or one with any activity younger than ``min_age_minutes``, is
    never selected.
    """
    moment = now or datetime.now(tz=UTC)
    cutoff = moment - timedelta(minutes=min_age_minutes)
    stale: list[str] = []
    for project in _reapable_candidates(owned):
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
    owned: OwnedStacks,
    *,
    min_age_minutes: int,
    now: "datetime | None" = None,
) -> list[ReapResult]:
    """Tear down the stale abandoned compose projects (see :func:`stale_compose_projects`)."""
    results: list[ReapResult] = []
    for project in stale_compose_projects(owned, min_age_minutes=min_age_minutes, now=now):
        result = reap_compose_project(project)
        if not result.is_noop:
            results.append(result)
    return results
