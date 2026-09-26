"""Check the running stack's create-time GPG environment."""

import os
import subprocess  # noqa: S404 — imported only for the SubprocessError type caught below; shell-outs go through teatree.utils.run
from collections.abc import Callable, Mapping

import typer

from teatree.utils.run import run_allowed_to_fail

#: Where ``deploy/docker-compose.yml`` bind-mounts the host GPG home. A container path,
#: and every mount TARGET in that file is fixed; used only when a container names no
#: ``TEATREE_HOST_GNUPG_DIR`` of its own, which is itself the pre-fix image's signature.
HOST_GNUPG_MOUNT = "/home/teatree/.gnupg"

#: The stack's compose project, matching ``deploy/t3``'s own resolution.
_PROJECT_ENV = "COMPOSE_PROJECT_NAME"
_PROJECT_DEFAULT = "teatree"
_HOST_GNUPG_DIR_ENV = "TEATREE_HOST_GNUPG_DIR"
_DOCKER_TIMEOUT_SECONDS = 20

type ContainerEnvironments = dict[str, dict[str, str]]


def _docker_stdout(argv: list[str]) -> str | None:
    """A read-only docker probe's stdout, or ``None`` when docker could not answer.

    A missing binary, an unreachable socket, a wedged daemon and a non-zero exit all
    collapse to ``None``, which the callers report as no finding rather than as health.
    """
    try:
        result = run_allowed_to_fail(argv, expected_codes=None, timeout=_DOCKER_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _running_stack_environments() -> ContainerEnvironments | None:
    """Each running stack container's create-time environment, or ``None`` if unanswerable."""
    project = os.environ.get(_PROJECT_ENV, "").strip() or _PROJECT_DEFAULT
    listing = _docker_stdout(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            "status=running",
            "--format",
            "{{.ID}} {{.Names}}",
        ]
    )
    if listing is None:
        return None
    environments: ContainerEnvironments = {}
    for row in listing.splitlines():
        container_id, _, name = row.strip().partition(" ")
        if not container_id:
            continue
        rendered = _docker_stdout(
            ["docker", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", container_id]
        )
        if rendered is None:
            return None
        environments[name or container_id] = dict(
            line.partition("=")[::2] for line in rendered.splitlines() if "=" in line
        )
    return environments


def _gnupg_home_problem(env: Mapping[str, str]) -> str | None:
    """Why *env* opens the host keybox, or ``None`` when it resolves a container-local home."""
    host_dir = env.get(_HOST_GNUPG_DIR_ENV, "").strip() or HOST_GNUPG_MOUNT
    home = env.get("GNUPGHOME", "").strip()
    if not home:
        return f"carries no GNUPGHOME, so gpg falls back to the host keybox at {host_dir}"
    if home == host_dir or home.startswith(f"{host_dir}/"):
        return f"starts from GNUPGHOME={home}, which is the host keybox mount"
    return None


def check_deployed_gnupg_home(*, probe: Callable[[], ContainerEnvironments | None] | None = None) -> bool:
    """FAIL when a RUNNING stack container would open the host GPG keybox."""
    environments = (probe or _running_stack_environments)()
    if not environments:
        return True
    drifted = {name: problem for name, env in environments.items() if (problem := _gnupg_home_problem(env))}
    if not drifted:
        return True
    for name, problem in drifted.items():
        typer.echo(f"FAIL  The running container {name} {problem}.")
    typer.echo(
        "      Every `docker exec` into it takes the host keybox lock, which gpg cannot "
        "reclaim across a PID namespace — signed commits, `pass show` and every t3 GitLab "
        "write block on it. Compose pins GNUPGHOME at create time, so recreating the stack "
        "repairs this with no image rebuild: `docker compose -f deploy/docker-compose.yml up -d`."
    )
    return False


__all__ = ["HOST_GNUPG_MOUNT", "check_deployed_gnupg_home"]
