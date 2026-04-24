"""Pure data types shared across teatree modules.

These types have no Django dependencies and no imports from ``teatree.core``,
so they can be used by any layer without introducing cycles.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict


@dataclass(frozen=True)
class RunCommand:
    """Structured run command with explicit working directory."""

    args: list[str] = field(default_factory=list)
    cwd: Path | None = None


type RunCommands = dict[str, list[str] | RunCommand]


class SymlinkSpec(TypedDict, total=False):
    path: str
    source: str
    mode: str
    description: str


class ServiceSpec(TypedDict, total=False):
    shared: bool
    service: str
    compose_file: str
    start_command: list[str]
    readiness_check: str
    base_image: str
    """Name of a ``BaseImageConfig`` the service's container should use.

    Teatree resolves this to a lockfile-hashed tag at ``worktree provision`` and
    exports it as a compose env var so ``image: ${...}`` substitution works.
    """


@dataclass(frozen=True, slots=True)
class BaseImageConfig:
    """Declares a Docker image teatree builds once and shares across worktrees.

    Teatree tags each image as ``{image_name}:deps-{sha256(lockfile)[:12]}`` —
    rebuild happens only when the lockfile content changes.  Code changes are
    picked up automatically via the worktree's ``.:/app`` volume mount, with
    no rebuild.

    *build_context* is an absolute path (the overlay resolves it — usually
    the main-repo root for that image's repo).  *dockerfile* and *lockfile*
    are resolved relative to it.  *env_var* is the name core exports into
    the per-worktree env cache with the resolved tag as value, so compose
    files can reference ``image: ${env_var}``.
    """

    image_name: str
    dockerfile: str
    lockfile: str
    build_context: Path
    env_var: str
    build_args: dict[str, str] = field(default_factory=dict)


class DbImportStrategy(TypedDict, total=False):
    kind: str
    source_database: str
    shared_postgres: bool
    snapshot_tool: str
    restore_order: list[str]
    notes: list[str]
    worktree_repo_path: str


class SkillMetadata(TypedDict, total=False):
    skill_path: str
    remote_patterns: list[str]
    trigger_index: list[dict[str, object]]
    resolved_requires: dict[str, list[str]]
    skill_mtimes: dict[str, int]
    teatree_version: str


class ToolCommand(TypedDict, total=False):
    name: str
    help: str
    command: str
    arguments: list[str]


class ValidationResult(TypedDict):
    errors: list[str]
    warnings: list[str]


@dataclass(frozen=True, slots=True)
class ProvisionStep:
    name: str
    callable: Callable[[], None]
    required: bool = True
    description: str = ""
