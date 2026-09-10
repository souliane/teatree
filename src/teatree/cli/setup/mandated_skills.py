"""Provision the configuration-mandated skills during ``t3 setup`` (#3652).

``apm`` is the declared installer, but it is absent from the deployed image and
its absence was only a WARN — so the mandated companion skills never landed on a
fresh box and nothing reported it. This step re-reads the same declaration the
doctor gate enumerates and installs every mandated skill that is not already
loadable — from the plugin's own ``skills/`` tree when it carries one (#3668), else
from the source repo the declaration names.

Idempotent by construction: a skill already resolvable on the runtime skills dir
is skipped, so re-running ``t3 setup`` (which the container entrypoint does on
every start) converges to the same state without touching the filesystem.
"""

from collections.abc import Callable
from pathlib import Path

from teatree.provisioning.declared import (
    DeclarationUnreadableError,
    DeclaredDependency,
    bundles_declared_in_apm_manifest,
    skills_declared_in_apm_manifest,
)
from teatree.provisioning.excluded_skills import CORE_EXCLUDED_SKILLS
from teatree.provisioning.probes import bundle_is_provisioned, skill_is_provisioned
from teatree.provisioning.skill_bundle import MandatedBundleInstaller
from teatree.provisioning.skill_source import InstallOutcome, MandatedSkillInstaller

type Echo = Callable[[str], None]


class MandatedSkillProvisioner:
    """Install every declared-but-absent mandated skill into the runtime skills dir."""

    def __init__(self, repo: Path, skills_dir: Path, cache_root: Path) -> None:
        self.repo = repo
        self.skills_dir = skills_dir
        self.cache_root = cache_root

    def provision(self, echo: Echo) -> bool:
        """Provision the declared mandated skills and bundles; ``False`` when any is still absent."""
        manifest = self.repo / "apm.yml"
        try:
            declared = skills_declared_in_apm_manifest(manifest)
            bundles = bundles_declared_in_apm_manifest(manifest)
        except DeclarationUnreadableError as exc:
            echo(f"WARN  Mandated-skill provisioning skipped: {exc}")
            return True

        return self._provision_skills(declared, echo) & self._provision_bundles(bundles, echo)

    def _provision_skills(self, declared: list[DeclaredDependency], echo: Echo) -> bool:
        search_dirs = [self.skills_dir]
        pending = [dep for dep in declared if not skill_is_provisioned(dep.name, search_dirs)]
        if not pending:
            echo(f"OK    Mandated skills: {len(declared)} declared, all already loadable.")
            return True

        self.skills_dir.mkdir(parents=True, exist_ok=True)
        installer = MandatedSkillInstaller(self.cache_root, plugin_skills_dir=self.repo / "skills")
        return self._install_each(pending, installer, echo)

    def _provision_bundles(self, bundles: list[DeclaredDependency], echo: Echo) -> bool:
        """Install every skill each declared bundle publishes (#4677).

        Reported per bundle whatever the outcome — a REQUIRED entry that no
        installer handled used to appear in setup's output as nothing at all.
        """
        if not bundles:
            return True
        search_dirs = [self.skills_dir]
        installer = MandatedBundleInstaller(self.cache_root, excluded=CORE_EXCLUDED_SKILLS)
        installed_all = True
        for bundle in bundles:
            if bundle_is_provisioned(bundle, search_dirs, cache_root=self.cache_root):
                echo(f"OK    Mandated bundle {bundle.name!r}: every skill it publishes is already loadable.")
                continue
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            report = installer.ensure(bundle, link_dir=self.skills_dir)
            echo(report.render())
            installed_all &= not report.unavailable
        return installed_all

    def _install_each(
        self,
        pending: list[DeclaredDependency],
        installer: MandatedSkillInstaller,
        echo: Echo,
    ) -> bool:
        unresolved: list[str] = []
        for dependency in pending:
            outcome = installer.ensure(dependency, link_dir=self.skills_dir)
            if outcome is InstallOutcome.UNAVAILABLE:
                unresolved.append(dependency.name)
            else:
                echo(f"OK    Mandated skill '{dependency.name}' provisioned from {dependency.source}.")
        for name in unresolved:
            echo(
                f"WARN  Mandated skill '{name}' could not be provisioned from its declared source — "
                "`t3 doctor check` will FAIL on it until the source is reachable."
            )
        return not unresolved
