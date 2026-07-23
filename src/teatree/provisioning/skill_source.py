"""Install a mandated skill from the source its declaration names (#3652).

``apm`` is the primary installer, but it is absent from the deployed image, and
its absence was a WARN — so the mandated companion skills never landed and
nothing said so. This is the fallback that makes ``t3 setup`` provision them
anyway, idempotently, from two sources in order:

1.  the PLUGIN's own ``skills/`` tree, when it already carries a skill by that
    name (#3668) — no network, no cache, deterministic. This is how the
    recommended set (the software-architecture skill above all) installs on a box
    with no reachable source repo.
2.  the declared source repo — cloned into a cache and symlinked from its declared
    subpath, for a skill the plugin does not carry.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from teatree.provisioning.declared import DeclaredDependency
from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)

_CLONE_TIMEOUT_SECONDS = 120
_MIN_SPEC_SEGMENTS = 3


class InstallOutcome(Enum):
    """What :meth:`MandatedSkillInstaller.ensure` did."""

    ALREADY_PRESENT = "already-present"
    INSTALLED = "installed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SkillSource:
    """The repo, subpath, and pinned ref a skill declaration names."""

    owner_repo: str
    subpath: str
    ref: str

    @property
    def cache_name(self) -> str:
        """The checkout directory name — per repo AND ref.

        Two skills from the same repo pinned to different refs need two
        checkouts; one shared directory would leave both symlinks tracking
        whichever ref was checked out last.
        """
        return f"{self.owner_repo.replace('/', '-')}@{self.ref or 'default'}"


def parse_skill_source(spec: str) -> SkillSource | None:
    """Split ``<owner>/<repo>/<subpath>[#<ref>]``; ``None`` when it names no single skill."""
    body, _, ref = spec.partition("#")
    segments = body.strip("/").split("/")
    if len(segments) < _MIN_SPEC_SEGMENTS:
        return None
    return SkillSource(
        owner_repo="/".join(segments[:2]),
        subpath="/".join(segments[2:]),
        ref=ref.strip(),
    )


def _link(link: Path, target: Path) -> None:
    """Point *link* at *target*, replacing whatever was there."""
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)


class MandatedSkillInstaller:
    """Provision declared skills from their source repos into a runtime skills dir."""

    def __init__(
        self,
        cache_root: Path,
        *,
        remote_base: str = "https://github.com/",
        plugin_skills_dir: Path | None = None,
    ) -> None:
        self.cache_root = cache_root
        self.remote_base = remote_base
        self.plugin_skills_dir = plugin_skills_dir

    def ensure(self, dependency: DeclaredDependency, *, link_dir: Path) -> InstallOutcome:
        """Make *dependency* loadable from *link_dir*, doing nothing when it already is."""
        if (link_dir / dependency.name / "SKILL.md").is_file():
            return InstallOutcome.ALREADY_PRESENT
        carried = self._plugin_copy(dependency.name)
        if carried is not None:
            _link(link_dir / dependency.name, carried)
            return InstallOutcome.INSTALLED
        source = parse_skill_source(dependency.source)
        if source is None:
            return InstallOutcome.UNAVAILABLE
        checkout = self._checkout(source)
        if checkout is None:
            return InstallOutcome.UNAVAILABLE
        target = checkout / source.subpath
        if not (target / "SKILL.md").is_file():
            return InstallOutcome.UNAVAILABLE
        _link(link_dir / dependency.name, target)
        return InstallOutcome.INSTALLED

    def _plugin_copy(self, name: str) -> Path | None:
        """The plugin's own copy of *name*, when it ships one."""
        if self.plugin_skills_dir is None:
            return None
        candidate = self.plugin_skills_dir / name
        return candidate if (candidate / "SKILL.md").is_file() else None

    def _checkout(self, source: SkillSource) -> Path | None:
        """Return the source repo's local checkout, cloning it once when absent."""
        destination = self.cache_root / source.cache_name
        if not (destination / ".git").exists():
            self.cache_root.mkdir(parents=True, exist_ok=True)
            url = f"{self.remote_base}{source.owner_repo}"
            result = run_allowed_to_fail(
                ["git", "clone", "--quiet", url, str(destination)],
                expected_codes=None,
                timeout=_CLONE_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                logger.warning("Could not clone declared skill source %s: %s", url, result.stderr.strip())
                return None
        if source.ref:
            checkout = run_allowed_to_fail(
                ["git", "-C", str(destination), "checkout", "--quiet", source.ref],
                expected_codes=None,
                timeout=_CLONE_TIMEOUT_SECONDS,
            )
            if checkout.returncode != 0:
                logger.warning(
                    "Declared skill source %s has no ref %s: %s",
                    source.owner_repo,
                    source.ref,
                    checkout.stderr.strip(),
                )
                return None
        return destination
