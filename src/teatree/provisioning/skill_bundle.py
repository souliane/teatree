"""Install a mandated WHOLE-REPO bundle from the source its declaration names (#4677).

:mod:`teatree.provisioning.skill_source` installs an entry that names ONE skill.
This is its sibling for the shape that names a REPO — ``obra/superpowers#<sha>``,
the manifest's only third-party mandate. That shape had no installer at all: the
skill enumeration dropped it because it names no single skill, so ``t3 setup``
never fetched it, ``t3 doctor check`` never probed it, and the three skills that
delegate their methodology to it resolved ``requires:`` to nothing on every
dispatch — warn-and-pass, indistinguishable from a present dependency.

The install is deliberately the same shape as its sibling: clone once into a
per-``(repo, ref)`` cache, check the declared pin out, and symlink each published
skill into the runtime skills dir. Two properties are load-bearing:

*   **A name already loadable is never displaced.** That keeps the step idempotent
    for the container entrypoint's every-start ``t3 setup``, and keeps a
    deliberately overridden local skill from being silently replaced.
*   **The exclusions are the runtime's, not this module's.** A bundle publishes
    skills teatree's multi-repo architecture conflicts with; the authority for that
    list is :data:`teatree.provisioning.excluded_skills.CORE_EXCLUDED_SKILLS`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from teatree.provisioning.declared import DeclaredDependency
from teatree.provisioning.skill_source import SkillSource, checkout_skill_source, link_skill

_BUNDLE_SPEC_SEGMENTS = 2
_SKILL_FILE = "SKILL.md"
_SKILLS_DIR = "skills"
_MAX_NAMED = 8


class BundleInstallOutcome(Enum):
    """What :meth:`MandatedBundleInstaller.ensure` did."""

    ALREADY_PRESENT = "already-present"
    INSTALLED = "installed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class BundleInstall:
    """What one declared bundle contributed to a runtime skills dir."""

    name: str
    ref: str = ""
    installed: tuple[str, ...] = ()
    already_loadable: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    unavailable: str = ""

    @property
    def outcome(self) -> BundleInstallOutcome:
        if self.unavailable:
            return BundleInstallOutcome.UNAVAILABLE
        return BundleInstallOutcome.INSTALLED if self.installed else BundleInstallOutcome.ALREADY_PRESENT

    def render(self) -> str:
        """The one line ``t3 setup`` prints for this bundle."""
        if self.unavailable:
            return (
                f"WARN  Mandated bundle {self.name!r} could not be provisioned: {self.unavailable} — "
                "`t3 doctor check` will FAIL on it until the source is reachable."
            )
        if not self.installed:
            return f"OK    Mandated bundle {self.name!r}: {len(self.already_loadable)} skill(s) already loadable."
        sample = ", ".join(self.installed[:_MAX_NAMED])
        more = f" (+{len(self.installed) - _MAX_NAMED} more)" if len(self.installed) > _MAX_NAMED else ""
        return (
            f"OK    Mandated bundle {self.name!r} at {self.ref or 'its default ref'}: "
            f"installed {len(self.installed)} skill(s) — {sample}{more}; "
            f"{len(self.already_loadable)} already loadable, {len(self.excluded)} excluded."
        )


def parse_bundle_source(spec: str) -> SkillSource | None:
    """Split ``<owner>/<repo>[#<ref>]``; ``None`` when the spec names one skill instead."""
    body, _, ref = spec.partition("#")
    segments = body.strip("/").split("/")
    if len(segments) != _BUNDLE_SPEC_SEGMENTS:
        return None
    return SkillSource(owner_repo="/".join(segments), subpath="", ref=ref.strip())


def published_bundle_skills(checkout: Path) -> dict[str, Path]:
    """Skill name → its directory, for every skill *checkout* publishes.

    A bundle conventionally publishes under ``skills/``; a repo publishing at its
    root is read too, so the answer comes from what is on disk rather than from an
    assumption about one upstream's layout.
    """
    for root in (checkout / _SKILLS_DIR, checkout):
        published = {entry.name: entry for entry in sorted(root.glob("*")) if (entry / _SKILL_FILE).is_file()}
        if published:
            return published
    return {}


class MandatedBundleInstaller:
    """Provision every skill a declared bundle publishes into a runtime skills dir."""

    def __init__(
        self,
        cache_root: Path,
        *,
        remote_base: str = "https://github.com/",
        excluded: Sequence[str] = (),
    ) -> None:
        self.cache_root = cache_root
        self.remote_base = remote_base
        self.excluded = tuple(excluded)

    def ensure(self, dependency: DeclaredDependency, *, link_dir: Path) -> BundleInstall:
        """Make every skill *dependency* publishes loadable from *link_dir*."""
        source = parse_bundle_source(dependency.source)
        if source is None:
            return BundleInstall(
                name=dependency.name,
                unavailable=f"{dependency.source!r} names no fetchable bundle repo",
            )
        checkout = checkout_skill_source(source, cache_root=self.cache_root, remote_base=self.remote_base)
        if checkout is None:
            return BundleInstall(
                name=dependency.name,
                ref=source.ref,
                unavailable=f"{source.owner_repo} at {source.ref or 'its default ref'} could not be fetched",
            )
        published = published_bundle_skills(checkout)
        if not published:
            return BundleInstall(
                name=dependency.name,
                ref=source.ref,
                unavailable=f"{source.owner_repo} at {source.ref or 'its default ref'} publishes no SKILL.md",
            )
        return self._link_each(dependency.name, source, published, link_dir=link_dir)

    def _link_each(
        self,
        name: str,
        source: SkillSource,
        published: dict[str, Path],
        *,
        link_dir: Path,
    ) -> BundleInstall:
        link_dir.mkdir(parents=True, exist_ok=True)
        installed: list[str] = []
        already: list[str] = []
        excluded: list[str] = []
        for skill, target in published.items():
            if skill in self.excluded:
                excluded.append(skill)
            elif (link_dir / skill / _SKILL_FILE).is_file():
                already.append(skill)
            else:
                link_skill(link_dir / skill, target)
                installed.append(skill)
        return BundleInstall(
            name=name,
            ref=source.ref,
            installed=tuple(installed),
            already_loadable=tuple(already),
            excluded=tuple(excluded),
        )
