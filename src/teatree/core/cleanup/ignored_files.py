"""Ignored checkout files that cannot be recreated from known provisioning inputs."""

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


def is_orchestration_debris(path: str) -> bool:
    return path.startswith(ORCHESTRATION_DEBRIS_PREFIXES)


def _regenerable(checkout: Path, relative: str) -> bool:
    parts = Path(relative).parts
    if is_orchestration_debris(relative):
        return True
    if any(part == "__pycache__" or artifact_name_pattern(part) for part in parts):
        return True
    if parts and parts[0].startswith(".coverage"):
        return True
    if relative == ".envrc" and EnvrcProvenance.is_generated(checkout):
        return True
    return any(is_tagged_cache(checkout.joinpath(*parts[:index])) for index in range(1, len(parts)))


def ignored_unregenerable(checkout: Path) -> list[str]:
    output = git.run_strict_verbatim(
        repo=str(checkout), args=["ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"]
    )
    paths: list[str] = []
    for entry in filter(None, output.split("\0")):
        if _regenerable(checkout, entry.rstrip("/")):
            continue
        candidate = checkout / entry
        if candidate.is_dir() and not candidate.is_symlink():
            for path in candidate.rglob("*"):
                relative = path.relative_to(checkout).as_posix()
                if (path.is_file() or path.is_symlink()) and not _regenerable(checkout, relative):
                    paths.append(relative)
        else:
            paths.append(entry)
    return sorted(set(paths))
