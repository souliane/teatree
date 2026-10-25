"""Ignored checkout files that cannot be recreated from known provisioning inputs."""

import os
from pathlib import Path

from teatree.core.cleanup.artifact_lock import artifact_name_pattern
from teatree.core.cleanup.checkout_registry import is_tagged_cache
from teatree.core.worktree.worktree_env import CACHE_DIRNAME, CACHE_FILENAME, EnvrcProvenance
from teatree.utils import git

ORCHESTRATION_DEBRIS_PREFIXES = (
    CACHE_FILENAME,
    f"{CACHE_DIRNAME}/",
    ".claude/worktrees/",
    ".secfix/",
    ".fix-bug/",
    ".worker-status/",
    ".claude-prompt",
    ".commit-message.txt",
)

REGENERABLE_ROOTS = frozenset({".local", "plugins", "site"})
REGENERABLE_FILES = frozenset({"ARCHITECTURE.md", ".pr-body.md"})


def is_orchestration_debris(path: str) -> bool:
    return path.startswith(ORCHESTRATION_DEBRIS_PREFIXES)


def _regenerable(checkout: Path, relative: str, tagged_cache: dict[Path, bool]) -> bool:
    parts = Path(relative).parts
    if (parts and (parts[0] in REGENERABLE_ROOTS or relative in REGENERABLE_FILES)) or is_orchestration_debris(
        relative
    ):
        return True
    if any(part == "__pycache__" or artifact_name_pattern(part) for part in parts):
        return True
    if parts and parts[0].startswith(".coverage"):
        return True
    if relative == ".envrc" and EnvrcProvenance.is_generated(checkout):
        return True
    for index in range(1, len(parts)):
        ancestor = checkout.joinpath(*parts[:index])
        if ancestor not in tagged_cache:
            tagged_cache[ancestor] = is_tagged_cache(ancestor)
        if tagged_cache[ancestor]:
            return True
    return False


def _raise_walk_error(exc: OSError) -> None:
    raise exc


def ignored_unregenerable(checkout: Path) -> list[str]:
    output = git.run_strict_verbatim(
        repo=str(checkout), args=["ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"]
    )
    paths: list[str] = []
    tagged_cache: dict[Path, bool] = {}
    for entry in filter(None, output.split("\0")):
        if _regenerable(checkout, entry.rstrip("/"), tagged_cache):
            continue
        candidate = checkout / entry
        if candidate.is_dir() and not candidate.is_symlink():
            for root, dirs, files in os.walk(candidate, onerror=_raise_walk_error):
                for name in files + [name for name in dirs if (Path(root) / name).is_symlink()]:
                    path = Path(root) / name
                    relative = path.relative_to(checkout).as_posix()
                    if not _regenerable(checkout, relative, tagged_cache):
                        paths.append(relative)
        else:
            paths.append(entry)
    return sorted(set(paths))
