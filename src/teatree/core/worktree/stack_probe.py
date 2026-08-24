"""Ask the DAEMON what a compose project is actually running.

The control plane records what teatree *intended* — an FSM state, a services
list. Only the daemon knows what survived. Every caller that needs the
difference asks through here, so "is this stack up?" has one answer and one
failure mode.

The probe keys on the compose PROJECT NAME, never on a worktree path: a project
name means the same thing in every venue, while a path does not (see
:mod:`teatree.core.worktree.venue`). An unanswerable daemon is reported as
``None`` rather than as an empty result, so a caller can fail closed instead of
reading "docker did not answer" as "nothing is running" — the difference between
keeping a live stack and reaping it.
"""

import logging
import subprocess  # noqa: S404 — imported only for the SubprocessError type caught below; shell-outs go through teatree.utils.run

from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)

DOCKER_PROBE_TIMEOUT_SECONDS = 10.0


def docker_probe(cmd: list[str]) -> str | None:
    """The combined output of a read-only docker probe, or ``None`` when docker could not answer.

    A missing binary, an unreachable daemon socket, a wedged daemon (timeout) and
    a non-zero exit all collapse to ``None``. Both streams are joined because
    ``docker logs`` forwards the container's stdout and stderr separately and a
    dev app server logs its requests on stderr.
    """
    try:
        result = run_allowed_to_fail(cmd, expected_codes=None, timeout=DOCKER_PROBE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout + result.stderr if result.returncode == 0 else None


def running_container_ids(project: str) -> list[str] | None:
    """The running container ids of *project*, or ``None`` when docker could not answer."""
    output = docker_probe(
        ["docker", "ps", "--filter", f"label=com.docker.compose.project={project}", "--format", "{{.ID}}"]
    )
    if output is None:
        return None
    return [line.strip() for line in output.splitlines() if line.strip()]
