from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import cast

from teatree.harness_skills import SkillsHarness
from teatree.provisioning.declared import (
    TEATREE_REPOSITORY,
    DeclarationUnreadableError,
    skills_declared_in_apm_manifest,
)
from teatree.provisioning.skill_pin import DEFAULT_REMOTE_BASE, refs_named
from teatree.provisioning.skill_source import SkillSource, parse_skill_source, pinned_commit
from teatree.provisioning.skills_cli import SkillAddResult, SkillAddStatus, SkillsCli, SkillsCliError

type Echo = Callable[[str], None]

_HARNESSES = (SkillsHarness.CLAUDE_CODE, SkillsHarness.CODEX)


class MandatedSkillProvisioner:
    def __init__(self, repo: Path, *, cli: SkillsCli | None = None, remote_base: str = DEFAULT_REMOTE_BASE) -> None:
        self.repo = repo
        self.cli = cli or SkillsCli()
        self.remote_base = remote_base

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

        succeeded = True
        grouped: OrderedDict[tuple[str, str], list[str]] = OrderedDict()
        for dependency in declared:
            source = cast("SkillSource", parse_skill_source(dependency.source))
            if source.owner_repo == TEATREE_REPOSITORY:
                continue
            if not (commit := pinned_commit(dependency.source)):
                echo(f"WARN  Skill '{dependency.name}' not installed: `{dependency.source}` has no 40-hex commit")
                succeeded = False
                continue
            key = (f"{source.owner_repo}#{commit}", source.remote_url(self.remote_base))
            grouped.setdefault(key, []).append(dependency.name)

        for (package, url), names in grouped.items():
            if reason := self._refusal(package, url):
                echo(f"WARN  Mandated skills from {package} not installed: {reason}.")
                succeeded = False
                continue
            try:
                results = self.cli.add_selected(package, _HARNESSES, tuple(names))
            except SkillsCliError as error:
                echo(f"WARN  Mandated skills from {package} could not be provisioned: {error}")
                succeeded = False
                continue
            succeeded = self._report(results, echo) and succeeded
        return succeeded

    @staticmethod
    def _refusal(package: str, url: str) -> str:
        commit = package.partition("#")[2]
        refs = refs_named(url, commit)
        if refs is None:
            return f"{url} is unreadable (a private source cannot be proved); the installed copy is left unchanged"
        return f"{url} has {', '.join(refs)} named like the pin, so the CLI installs its tip" if refs else ""

    @staticmethod
    def _report(results: tuple[SkillAddResult, ...], echo: Echo) -> bool:
        succeeded = True
        for result in results:
            if result.status is SkillAddStatus.INSTALLED:
                echo(f"OK    Mandated skill '{result.name}' installed for Claude Code and Codex.")
            else:
                echo(f"WARN  Mandated skill '{result.name}' could not be provisioned by the skills CLI.")
                succeeded = False
        return succeeded
