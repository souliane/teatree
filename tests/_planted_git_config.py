"""A checkout whose git config carries commands an agent could have planted.

``core.fsmonitor`` names a command git runs while it reads the index; ``core.hooksPath`` names a directory
whose hooks git runs on checkout, merge and ref updates. Every planted script appends its name to one marker
file, so a test asserts on what actually ran rather than on what was configured.
"""

import stat
from dataclasses import dataclass
from pathlib import Path

from tests._git_repo import run_git

PLANTED_HOOK_NAMES = ("post-checkout", "post-merge", "reference-transaction")


@dataclass(frozen=True)
class PlantedConfig:
    marker: Path
    hooks_dir: Path

    @property
    def fired(self) -> list[str]:
        return self.marker.read_text(encoding="utf-8").split() if self.marker.exists() else []


def executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def recording_script(path: Path, marker: Path, name: str) -> None:
    executable(path, f"echo {name} >> '{marker}'\nexit 0")


def plant_exec_config(repo: Path, scratch: Path) -> PlantedConfig:
    """Point *repo*'s ``core.fsmonitor`` and ``core.hooksPath`` at recording scripts kept under *scratch*."""
    marker = scratch / "marker"
    fsmonitor = scratch / "fsmonitor.sh"
    recording_script(fsmonitor, marker, "fsmonitor")
    hooks_dir = scratch / "planted-hooks"
    for name in PLANTED_HOOK_NAMES:
        recording_script(hooks_dir / name, marker, name)
        recording_script(repo / ".git" / "hooks" / name, marker, f"default-{name}")
    run_git(repo, "config", "core.fsmonitor", str(fsmonitor))
    run_git(repo, "config", "core.hooksPath", str(hooks_dir))
    return PlantedConfig(marker=marker, hooks_dir=hooks_dir)
