"""Safe disk reclaim: only the three zero-data-loss Docker prunes, never ``-a``.

On a "free disk space" request the safe reclaims are build cache (rebuildable,
usually the largest), DANGLING-only images, and UNREFERENCED-only volumes. These
never touch a running stack, a tagged application image, or an attached DB volume
backing a live worktree.

The danger this module forecloses is the ``-a`` blast: ``docker image prune -af``
removes every unused image including the application images (forcing full
rebuilds), and pruning just after a stack is stopped makes that stack's images
"unused" so ``-af`` reaps them. The argv each step passes is fixed and asserted
in tests — ``-a`` / ``--all`` / ``system prune`` can never enter the reclaim set.

This is THE sanctioned disk-reclaim path. Removing application images or tearing
down worktrees/DBs stays a separate, explicitly-targeted action (``workspace
teardown`` / ``clean-all``), never bundled here.

Absence and failure are different, and the difference is load-bearing. A MISSING
docker binary (CI sandboxes, hermetic tests) never crashes the caller. A docker
that ANSWERS and refuses — daemon down, or the socket mounted but not granted to
the service the CLI runs in — is a FAILURE: it is recorded on the step, marked
in ``render()``, and exits the command non-zero. Reporting either as a clean
``0B`` is indistinguishable from "nothing left to reclaim", and tells an
operator under disk pressure that the sanctioned path ran when it never did.

A VENUE THAT CANNOT ACT IS DECIDED UP FRONT (#4585). The daemon is probed before
anything is planned or run, because three prunes that cannot succeed produce a
wall of identical refusals and still no route out. What the operator on a full
disk needs instead is one line naming where the command DOES work: the teatree
stack mounts the docker socket into every service and grants it to
``teatree-worker`` alone, and that service also reads the control DB — so it is
the venue that can do both, and the refusal names it. Where no in-stack route
applies, the three prunes are printed verbatim for a host that reaches dockerd,
derived from the reclaim set itself so the advice can never drift from what the
command would have run.
"""

import logging
import re
from dataclasses import dataclass

from teatree.docker.venue import DockerVenue, docker_venue
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

logger = logging.getLogger(__name__)

_PRUNE_TIMEOUT = 300

# docker image/volume prune print "Total reclaimed space: <size>"; builder prune
# prints "Total:\t<size>". Both summary shapes are parsed to one size string.
_RECLAIMED_RE = re.compile(r"Total(?: reclaimed space)?:\s*([\d.]+\s*[a-zA-Z]*B)")
_SIZE_RE = re.compile(r"([\d.]+)\s*([a-zA-Z]*B)")
_SI_STEP = 1000  # docker reports SI (decimal) sizes: kB/MB/GB
_UNIT_FACTORS = {
    "B": 1,
    "KB": _SI_STEP,
    "MB": _SI_STEP**2,
    "GB": _SI_STEP**3,
    "TB": _SI_STEP**4,
    "PB": _SI_STEP**5,
}
_HUMAN_UNITS = ("B", "kB", "MB", "GB", "TB", "PB")


@dataclass(frozen=True, slots=True)
class PruneOutcome:
    reclaimed: str
    bytes_reclaimed: int
    failure: str | None = None


@dataclass(frozen=True, slots=True)
class ReclaimStep:
    argv: list[str]
    label: str
    outcome: PruneOutcome | None = None

    @property
    def summary_line(self) -> str:
        if self.outcome is None:
            return f"  {self.label}: reclaimed 0B"
        if self.outcome.failure is not None:
            return f"  {self.label}: FAILED — {self.outcome.failure}"
        return f"  {self.label}: reclaimed {self.outcome.reclaimed}"


@dataclass(frozen=True, slots=True)
class ReclaimReport:
    steps: tuple[ReclaimStep, ...]
    planned: tuple[ReclaimStep, ...]
    dry_run: bool = False
    venue: DockerVenue | None = None

    @property
    def unreachable_venue(self) -> DockerVenue | None:
        """The venue when it cannot act, so each reader narrows once rather than per use."""
        if self.venue is None or self.venue.reachable:
            return None
        return self.venue

    @property
    def venue_blocked(self) -> bool:
        """No prune ran because this venue cannot reach dockerd.

        False for a dry run: it removes nothing by construction and says so, so
        it can never be misread as a reclaim that succeeded.
        """
        return self.unreachable_venue is not None and not self.dry_run

    @property
    def total_bytes(self) -> int:
        return sum(step.outcome.bytes_reclaimed for step in self.steps if step.outcome is not None)

    @property
    def total_human(self) -> str:
        return _human_bytes(self.total_bytes)

    @property
    def failures(self) -> tuple[tuple[str, str], ...]:
        """Label + reason for every step docker actively refused (never for a missing binary)."""
        return tuple(
            (step.label, step.outcome.failure)
            for step in self.steps
            if step.outcome is not None and step.outcome.failure is not None
        )

    def failure_summary(self) -> str:
        blocked = self.unreachable_venue
        if blocked is not None and self.venue_blocked:
            return f"docker is not reachable from {blocked.description} — {blocked.reason}"
        detail = "; ".join(f"{label}: {reason}" for label, reason in self.failures)
        return f"docker reclaim failed on {len(self.failures)} of {len(self.steps)} steps — {detail}"

    def render(self) -> str:
        blocked = self.unreachable_venue
        if self.dry_run:
            lines = ["Dry run — would reclaim (nothing removed):"]
            lines += [f"  {step.label}: {' '.join(step.argv)}" for step in self.planned]
            if blocked is not None:
                lines.append(f"This plan cannot execute in {blocked.description} — {blocked.reason}")
            return "\n".join(lines)
        if blocked is not None:
            return "\n".join(_unreachable_lines(blocked, self.planned))
        lines = [step.summary_line for step in self.steps]
        lines.append(f"Total reclaimed: {self.total_human}")
        return "\n".join(lines)


# Fixed reclaim set. The labels document intent; the argv is the safety boundary.
_SAFE_STEPS: tuple[tuple[list[str], str], ...] = (
    (["docker", "builder", "prune", "-af"], "build cache"),
    (["docker", "image", "prune", "-f"], "dangling images"),
    (["docker", "volume", "prune", "-f"], "unreferenced volumes"),
)


# The service `deploy/docker-compose.yml` grants the docker socket to, and which
# also reads the control DB — the one venue that can do both.
_GRANTED_SERVICE = "teatree-worker"


def _unreachable_lines(venue: DockerVenue, planned: tuple[ReclaimStep, ...]) -> list[str]:
    """The refusal, then the route out — a refusal with no route is the #4585 defect."""
    lines = [
        f"docker is not reachable from {venue.description} — the reclaim did not run.",
        f"  {venue.reason}",
        "Run it where the daemon answers:",
    ]
    in_stack_route = venue.containerized and venue.service_role != "worker"
    if in_stack_route:
        lines += [
            f"  {_GRANTED_SERVICE} is the one service granted the docker socket, and it reads the same control DB:",
            "    deploy/t3 <overlay> workspace reclaim-disk    # from the host — execs into the worker",
            f"    docker compose -f deploy/docker-compose.yml exec {_GRANTED_SERVICE} \\",
            "      t3 <overlay> workspace reclaim-disk",
        ]
    prefix = "  or run" if in_stack_route else "  run"
    lines.append(f"{prefix} exactly these three, in this order and nothing else, where docker answers:")
    lines += [f"    {' '.join(step.argv)}" for step in planned]
    return lines


def _parse_size(raw: str) -> int:
    match = _SIZE_RE.fullmatch(raw.strip())
    if not match:
        return 0
    value, unit = match.groups()
    factor = _UNIT_FACTORS.get(unit.upper(), 0)
    return int(float(value) * factor)


def _human_bytes(total: int) -> str:
    size = float(total)
    *scaled_units, top_unit = _HUMAN_UNITS
    for unit in scaled_units:
        if size < _SI_STEP:
            return f"{int(size)}B" if unit == "B" else f"{size:.1f}{unit}"
        size /= _SI_STEP
    return f"{size:.1f}{top_unit}"


def _extract_reclaimed(stdout: str) -> str:
    match = _RECLAIMED_RE.search(stdout)
    return match.group(1).replace(" ", "") if match else "0B"


def _run_prune(argv: list[str]) -> PruneOutcome:
    """Run one prune command; return its reclaimed size, or the reason it did not run.

    Never raises: a step that fails must not forfeit the reclaim the remaining
    steps can still do. An ABSENT docker binary is tolerated silently; a docker
    that answers and refuses records a ``failure`` the caller surfaces.
    """
    try:
        result = run_allowed_to_fail(argv, expected_codes=None, timeout=_PRUNE_TIMEOUT)
    except (FileNotFoundError, PermissionError) as exc:
        logger.debug("docker unavailable, skipping %s: %s", argv[:3], exc)
        return PruneOutcome(reclaimed="0B", bytes_reclaimed=0)
    except TimeoutExpired:
        logger.warning("docker prune timed out: %s", argv[:3])
        return PruneOutcome(reclaimed="0B", bytes_reclaimed=0, failure=f"timed out after {_PRUNE_TIMEOUT}s")
    if result.returncode != 0:
        reason = result.stderr.strip()[:300] or f"exit status {result.returncode}"
        logger.warning("docker %s failed: %s", argv[:3], reason)
        return PruneOutcome(reclaimed="0B", bytes_reclaimed=0, failure=reason)
    reclaimed = _extract_reclaimed(result.stdout)
    return PruneOutcome(reclaimed=reclaimed, bytes_reclaimed=_parse_size(reclaimed))


def reclaim_disk(*, dry_run: bool = False) -> ReclaimReport:
    """Reclaim disk via the three safe Docker prunes; report per-step + total.

    ``dry_run`` plans the reclaim set without running anything destructive — the
    ``planned`` steps carry the exact argv that would run.
    """
    planned = tuple(ReclaimStep(argv=list(argv), label=label) for argv, label in _SAFE_STEPS)
    venue = docker_venue()
    if dry_run or not venue.reachable:
        return ReclaimReport(steps=(), planned=planned, dry_run=dry_run, venue=venue)
    steps = tuple(ReclaimStep(argv=step.argv, label=step.label, outcome=_run_prune(step.argv)) for step in planned)
    return ReclaimReport(steps=steps, planned=planned, dry_run=False, venue=venue)
