from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import cast

from teatree.harness_skills import SkillsHarness
from teatree.provisioning.declared import DeclarationUnreadableError, skills_declared_in_apm_manifest
from teatree.provisioning.skill_source import SkillSource, parse_skill_source
from teatree.provisioning.skills_cli import SkillAddResult, SkillAddStatus, SkillsCli, SkillsCliError

type Echo = Callable[[str], None]

_HARNESSES = (SkillsHarness.CLAUDE_CODE, SkillsHarness.CODEX)
_TEATREE_REPOSITORY = "souliane/teatree"


class MandatedSkillProvisioner:
    def __init__(self, repo: Path, *, cli: SkillsCli | None = None) -> None:
        self.repo = repo
        self.cli = cli or SkillsCli()

    def provision(self, echo: Echo) -> bool:
        try:
            self.cli.version()
        except SkillsCliError as error:
            echo(f"WARN  Harness skills were not changed because the pinned skills CLI preflight failed: {error}")
            return False
        try:
            declared = skills_declared_in_apm_manifest(self.repo / "apm.yml")
        except DeclarationUnreadableError as error:
            echo(f"WARN  Mandated-skill provisioning skipped: {error}")
            return False

        grouped: OrderedDict[str, list[str]] = OrderedDict()
        for dependency in declared:
            source = cast("SkillSource", parse_skill_source(dependency.source))
            if source.owner_repo == _TEATREE_REPOSITORY:
                continue
            package = f"{source.owner_repo}{f'#{source.ref}' if source.ref else ''}"
            grouped.setdefault(package, []).append(dependency.name)

        if not grouped:
            return True

        succeeded = True
        for package, names in grouped.items():
            try:
                results = self.cli.add_selected(package, _HARNESSES, tuple(names))
            except SkillsCliError as error:
                echo(f"WARN  Mandated skills from {package} could not be provisioned: {error}")
                succeeded = False
                continue
            succeeded = self._report(results, echo) and succeeded
        return succeeded

    @staticmethod
    def _report(results: tuple[SkillAddResult, ...], echo: Echo) -> bool:
        succeeded = True
        for result in results:
            if result.status is SkillAddStatus.FAILED:
                echo(f"WARN  Mandated skill '{result.name}' could not be provisioned by the skills CLI.")
                succeeded = False
            elif result.status is SkillAddStatus.SKIPPED:
                echo(f"OK    Mandated skill '{result.name}' already installed for Claude Code and Codex.")
            else:
                echo(f"OK    Mandated skill '{result.name}' installed for Claude Code and Codex.")
        return succeeded
