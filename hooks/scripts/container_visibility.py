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
from the CLI container's own mount table: a bind mount whose destination equals
its source carries a path across the boundary unchanged, and nothing else does.

Absence of proof is not proof of absence, and here it must not be treated as
either — every unanswerable case (no docker, no running container, an unreadable
mount table) yields no roots, so the caller declares nothing and the refusal
stands.

Cold-import safe: a hook is a bare ``python3`` subprocess with no guarantee
``teatree`` is importable, so this imports only stdlib.
"""

import os
import shutil
import subprocess  # noqa: S404 — stdlib subprocess for the trusted local docker CLI
import sys
from functools import cache
from pathlib import Path

# Alias both identities so a bare and a package-qualified import resolve the SAME
# module object — the pattern every sibling leaf uses.
sys.modules.setdefault("container_visibility", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.container_visibility", sys.modules[__name__])

#: The compose service ``deploy/t3`` execs the CLI into, so its mounts are the ones
#: that decide the answer. Read from the same knob the wrapper reads, so an operator
#: who redirects the CLI cannot leave the two disagreeing about which container counts.
_CLI_SERVICE = os.environ.get("TEATREE_DOCKER_CLI_SERVICE", "teatree-worker")

#: Compose's marker for an ephemeral ``run --rm`` container. It shares the service
#: name with the long-lived one, and ``exec`` never lands in it — reading its mounts
#: would answer about a container the CLI does not run in.
_ONEOFF_LABEL = "com.docker.compose.oneoff"

_SERVICE_FORMAT = '{{.ID}} {{.Label "' + _ONEOFF_LABEL + '"}}'
_MOUNT_FORMAT = "{{range .Mounts}}{{.Source}}\t{{.Destination}}\n{{end}}"

#: Ample for a local daemon round-trip (measured ~46ms), and short because this cost
#: lands BEFORE the gate's own validator allowance starts — a probe that waited would
#: spend the hook's budget on a question whose unanswered form is already handled.
_PROBE_TIMEOUT = 1.0


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


def _cli_container() -> str | None:
    """The running, non-ephemeral container serving the CLI service, or ``None``."""
    listing = _docker(
        "ps",
        "--filter",
        f"label=com.docker.compose.service={_CLI_SERVICE}",
        "--filter",
        "status=running",
        "--format",
        _SERVICE_FORMAT,
    )
    for line in (listing or "").splitlines():
        container, _, oneoff = line.partition(" ")
        if container and oneoff.strip() != "True":
            return container
    return None


@cache
def identity_mount_roots() -> tuple[str, ...]:
    """Host roots the CLI container mounts at their own path.

    Cached for the life of the hook process: the mount table cannot change under a
    running container, and a hook that shells out repeatedly should pay one probe.
    """
    container = _cli_container()
    if container is None:
        return ()
    table = _docker("inspect", "--format", _MOUNT_FORMAT, container)
    if table is None:
        return ()
    roots = []
    for line in table.splitlines():
        source, separator, destination = line.partition("\t")
        if separator and source and source == destination:
            roots.append(source)
    return tuple(roots)


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
