"""Can THIS venue drive the docker daemon, and which venue is it (#4585)?

A command that needs dockerd has three possible relationships with it, and
collapsing any two of them produces a confidently wrong report:

- the daemon ANSWERS — act;
- the daemon REFUSES — this venue cannot act, and saying so is the whole value;
- there is no docker CLI here at all — a hermetic CI sandbox, where crashing is
wrong but claiming a completed no-op is worse.

Presence of ``/var/run/docker.sock`` cannot separate the first two. The teatree
stack mounts that socket into EVERY service and grants it to exactly one via
``group_add`` (see ``deploy/docker-compose.yml``), so in ``teatree-admin`` the
node is present and every ``connect(2)`` on it is denied. The probe therefore
asks the daemon rather than the filesystem.

The venue's own identity travels with the verdict because it is what makes a
refusal actionable: knowing the caller is the ``admin`` service is what lets a
caller say "re-run in the worker" instead of "permission denied".
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

logger = logging.getLogger(__name__)

# Short on purpose: a healthy daemon answers `version` in milliseconds, and the
# caller is typically an operator on a full disk who must not wait on a hang.
_PROBE_TIMEOUT = 15

# Read-only, and it needs the DAEMON — `docker version` alone would succeed
# against the client half with no daemon at all.
_PROBE_ARGV = ("docker", "version", "--format", "{{.Server.Version}}")

_CONTAINER_MARKER = Path("/.dockerenv")


@dataclass(frozen=True, slots=True)
class DockerVenue:
    """Whether this process can drive dockerd, and where "here" is."""

    reachable: bool
    reason: str = ""
    has_cli: bool = True
    containerized: bool = False
    service_role: str = ""

    @property
    def description(self) -> str:
        if self.service_role:
            return f"the {self.service_role} container"
        return "this container" if self.containerized else "this host"


def _identity() -> tuple[bool, str]:
    containerized = _CONTAINER_MARKER.exists()
    role = os.environ.get("TEATREE_ROLE", "").strip() if containerized else ""
    return containerized, role


def docker_venue() -> DockerVenue:
    """Probe the daemon read-only; never raises, so a caller under pressure cannot crash."""
    containerized, role = _identity()
    argv = list(_PROBE_ARGV)
    try:
        result = run_allowed_to_fail(argv, expected_codes=None, timeout=_PROBE_TIMEOUT)
    except FileNotFoundError as exc:
        return DockerVenue(
            reachable=False,
            reason=f"no docker CLI here ({exc})",
            has_cli=False,
            containerized=containerized,
            service_role=role,
        )
    except TimeoutExpired:
        reason = f"docker timed out after {_PROBE_TIMEOUT}s"
        return DockerVenue(reachable=False, reason=reason, containerized=containerized, service_role=role)
    except OSError as exc:
        # PermissionError lands here too: an unopenable socket, the admin shape.
        return DockerVenue(reachable=False, reason=str(exc), containerized=containerized, service_role=role)
    if result.returncode != 0:
        reason = result.stderr.strip()[:300] or f"exit status {result.returncode}"
        logger.debug("docker unreachable from %s: %s", role or "this venue", reason)
        return DockerVenue(reachable=False, reason=reason, containerized=containerized, service_role=role)
    return DockerVenue(reachable=True, containerized=containerized, service_role=role)
