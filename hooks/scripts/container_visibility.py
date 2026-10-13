"""Which host paths the containerized ``t3`` reaches under their own name.

``deploy/t3`` translates the host working directory into container coordinates and
REFUSES a checkout it cannot place, because an untranslated cwd degrades to the
image WORKDIR — where the container's own source tree sits — and a cwd-sensitive
command then resolves against a different tree and reports success. The refusal
names its own escape: ``TEATREE_INVOCATION_CWD`` short-circuits the translation
table "when you know this tree is reachable there".

"Know" is the load-bearing word, and this module is what supplies it. A blind
``TEATREE_INVOCATION_CWD=$(pwd)`` would assert reachability rather than establish
it, defeating the guard precisely where it earns its keep. So the answer comes
from the mount table of the container ``deploy/t3`` would exec into: a bind mount
whose destination equals its source carries a path across the boundary unchanged,
and nothing else does.

That container is chosen by asking the launcher's own question — its compose
project, its route order, its probe format — because any other container is
one the CLI never runs in. A second stack running the same service (a scratch
review stack) must not stand in for the production one.

Absence of proof is not proof of absence, and here it must not be treated as
either — every unanswerable case (no docker answer, no route container, several
of them, an unreadable mount table) yields no roots and a named reason, so the
caller declares nothing, the refusal stands, and the refusal can say why.

Cold-import safe: a hook is a bare ``python3`` subprocess with no guarantee
``teatree`` is importable, so this imports only stdlib.
"""

import os
import shlex
import shutil
import subprocess  # noqa: S404 — stdlib subprocess for the trusted local docker CLI
import sys
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path

# Alias both identities so a bare and a package-qualified import resolve the SAME
# module object — the pattern every sibling leaf uses.
sys.modules.setdefault("container_visibility", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.container_visibility", sys.modules[__name__])

#: ``deploy/t3``'s probe format, byte for byte, so one table answers the launcher and this prover alike.
PROBE_FORMAT = '{{.ID}} {{.Label "com.docker.compose.service"}} {{.Label "com.docker.compose.oneoff"}}'
_MOUNT_FORMAT = "{{range .Mounts}}{{.Source}}\t{{.Destination}}\n{{end}}"

#: Ample for a local daemon round-trip (measured ~46ms), and short because this cost
#: lands BEFORE the gate's own validator allowance starts — a probe that waited would
#: spend the hook's budget on a question whose unanswered form is already handled.
_PROBE_TIMEOUT = 1.0

_NO_ANSWER = (
    "docker gave no answer about {subject} — no docker binary, a daemon that is down, a permission error, or a timeout"
)
_NO_ROUTE = "compose project {project!r} runs no {services} container"
_SEVERAL = (
    "{count} running {service} containers in compose project {project!r} ({ids}); the deploy never runs two, so one "
    "was scaled or started outside it — `docker compose -p {project_arg} ps {service}` lists them"
)
_NOT_MOUNTED = (
    "container {container} of compose project {project!r} mounts no host directory at its own path holding "
    "{path} — the hook plugin is installed outside every such mount; install it from a checkout the container "
    "mounts at that same path"
)


@dataclass(frozen=True, slots=True)
class CliPick:
    """The one container ``deploy/t3`` would exec into, or why none can be named."""

    container: str = ""
    reason: str = ""
    identity_roots: tuple[str, ...] = ()


def _docker(*args: str) -> str | None:
    """The stdout of ``docker <args>``, or ``None`` when the daemon does not answer."""
    binary = shutil.which("docker")
    if binary is None:
        return None
    try:
        probe = subprocess.run(  # noqa: S603 — trusted local CLI; fixed argv, no shell
            [binary, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return probe.stdout if probe.returncode == 0 else None


def _project_variable() -> str | None:
    """The variable naming the production project, or ``None`` when it is the default."""
    return next((name for name in ("TEATREE_COMPOSE_PROJECT", "COMPOSE_PROJECT_NAME") if os.environ.get(name)), None)


def production_compose_project() -> str:
    """The compose project ``deploy/t3`` dispatches into, by its own precedence."""
    variable = _project_variable()
    return os.environ[variable] if variable else "teatree"


def cli_services() -> tuple[str, ...]:
    """``deploy/t3``'s route order: the CLI service, then its fallbacks, first mention wins."""
    preferred = os.environ.get("TEATREE_DOCKER_CLI_SERVICE") or "teatree-worker"
    fallbacks = os.environ.get("TEATREE_DOCKER_CLI_FALLBACK_SERVICES") or "teatree-admin teatree-worker"
    return tuple(dict.fromkeys(f"{preferred} {fallbacks}".split()))


def _route_containers(table: str) -> list[tuple[str, str]]:
    """``(container, service)`` per row ``exec`` could land in — a proven one-off never can."""
    routes = []
    for line in table.splitlines():
        container, service, oneoff = [*line.split(maxsplit=2), "", ""][:3]
        if container and oneoff.strip() != "True":
            routes.append((container, service))
    return routes


def cli_pick() -> CliPick:
    """The first route with a container, which must be the only one of its service."""
    project = production_compose_project()
    table = _docker(
        "ps",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--filter",
        "status=running",
        "--format",
        PROBE_FORMAT,
    )
    if table is None:
        return CliPick(reason=_NO_ANSWER.format(subject=f"compose project {project!r}"))
    routes = _route_containers(table)
    services = cli_services()
    for service in services:
        containers = [container for container, name in routes if name == service]
        if len(containers) == 1:
            return CliPick(container=containers[0])
        if containers:
            ids = ", ".join(containers)
            return CliPick(
                reason=_SEVERAL.format(
                    count=len(containers), service=service, project=project, ids=ids, project_arg=shlex.quote(project)
                )
            )
    return CliPick(reason=_NO_ROUTE.format(project=project, services=" or ".join(services)))


@cache
def proved_pick() -> CliPick:
    """:func:`cli_pick` with the picked container's identity mounts read.

    Cached for the life of the hook process: the mount table cannot change under a
    running container, and a hook that shells out repeatedly should pay one probe.
    """
    pick = cli_pick()
    if not pick.container:
        return pick
    table = _docker("inspect", "--format", _MOUNT_FORMAT, pick.container)
    if table is None:
        return replace(pick, reason=_NO_ANSWER.format(subject=f"the mounts of container {pick.container}"))
    roots = []
    for line in table.splitlines():
        source, separator, destination = line.partition("\t")
        if separator and source and source == destination:
            roots.append(source)
    return replace(pick, identity_roots=tuple(roots))


def identity_mount_roots() -> tuple[str, ...]:
    """Host roots the CLI container mounts at their own path."""
    return proved_pick().identity_roots


def container_path(path: Path) -> str | None:
    """*path* as the CLI container reaches it, or ``None`` when no mount proves it.

    Physical, because a mount source is: the container reaches the resolved
    spelling, never the symlink that led there.
    """
    resolved = path.resolve()
    for root in map(Path, identity_mount_roots()):
        if resolved == root or root in resolved.parents:
            return str(resolved)
    return None


def unreachable_reason(path: Path) -> str | None:
    """Why *path* cannot be declared to the CLI container, or ``None`` when a mount proves it."""
    pick = proved_pick()
    if container_path(path) is not None:
        return None
    reason = pick.reason or _NOT_MOUNTED.format(
        container=pick.container, project=production_compose_project(), path=path.resolve()
    )
    variable = _project_variable()
    return f"{reason} (project from {variable})" if variable else reason
