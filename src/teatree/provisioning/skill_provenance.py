from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from teatree.utils.run import SUBPROCESS_UNREACHABLE, CompletedProcess, run_allowed_to_fail

_GIT_TIMEOUT_SECONDS = 30


class SkillInstallKind(StrEnum):
    MISSING = "missing"
    COPY = "copy"
    SYMLINK = "symlink"
    BROKEN_SYMLINK = "broken-symlink"


class GitProbeState(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class GitProvenance:
    state: GitProbeState
    repo_path: str | None
    local_sha: str | None
    branch: str | None
    detached: bool | None
    default_branch: str | None
    default_sha: str | None
    ahead: int | None
    behind: int | None
    newer_on_default: bool | None
    diagnostic: str | None


@dataclass(frozen=True, slots=True)
class SkillInstallationFact:
    front_door_path: str
    kind: SkillInstallKind
    link_target: str | None
    git: GitProvenance | None


CommandRunner = Callable[[Sequence[str]], CompletedProcess[str]]


class SkillInstallationProber(Protocol):
    def probe(self, front_door: Path) -> SkillInstallationFact:
        raise NotImplementedError


class _GitProbeError(RuntimeError):
    def __init__(self, diagnostic: str, repo_path: str | None = None) -> None:
        self.diagnostic = diagnostic
        self.repo_path = repo_path
        super().__init__(diagnostic)


def _run_command(command: Sequence[str]) -> CompletedProcess[str]:
    return run_allowed_to_fail(command, expected_codes=None, timeout=_GIT_TIMEOUT_SECONDS)


@dataclass(slots=True)
class SkillProvenanceProbe:
    runner: CommandRunner = _run_command
    _repo_snapshots: dict[str, GitProvenance] = field(default_factory=dict, init=False, repr=False)

    def probe(self, front_door: Path) -> SkillInstallationFact:
        if not front_door.is_symlink():
            kind = SkillInstallKind.COPY if front_door.exists() else SkillInstallKind.MISSING
            return SkillInstallationFact(str(front_door), kind, None, None)
        try:
            link_target = str(front_door.readlink())
            resolved = front_door.resolve(strict=False)
            target_exists = resolved.exists()
        except OSError:
            return SkillInstallationFact(
                str(front_door),
                SkillInstallKind.SYMLINK,
                None,
                _unknown_git("symlink probe failed"),
            )
        if not target_exists:
            return SkillInstallationFact(
                str(front_door),
                SkillInstallKind.BROKEN_SYMLINK,
                link_target,
                _unknown_git("symlink target is missing"),
            )
        return SkillInstallationFact(
            str(front_door),
            SkillInstallKind.SYMLINK,
            link_target,
            self._probe_git(resolved),
        )

    def _probe_git(self, target: Path) -> GitProvenance:
        try:
            root = self._repository_root(target)
        except _GitProbeError as error:
            return _unknown_git(error.diagnostic, repo_path=error.repo_path)
        cache_key = str(root.resolve(strict=False))
        if cached := self._repo_snapshots.get(cache_key):
            return cached
        try:
            self._fetch_origin(root)
            local_sha, branch, default_branch = self._head_state(root)
            default_sha, ahead, behind = self._default_comparison(root, default_branch)
        except _GitProbeError as error:
            provenance = _unknown_git(error.diagnostic, repo_path=error.repo_path)
        except ValueError:
            provenance = _unknown_git("git provenance probe failed", repo_path=str(root))
        else:
            provenance = GitProvenance(
                state=GitProbeState.KNOWN,
                repo_path=str(root),
                local_sha=local_sha,
                branch=branch,
                detached=branch is None,
                default_branch=default_branch,
                default_sha=default_sha,
                ahead=ahead,
                behind=behind,
                newer_on_default=behind > 0,
                diagnostic=None,
            )
        self._repo_snapshots[cache_key] = provenance
        return provenance

    def _repository_root(self, target: Path) -> Path:
        root_result = self._git(target, "rev-parse", "--show-toplevel")
        if root_result is None or root_result.returncode != 0 or not root_result.stdout.strip():
            diagnostic = "git repository probe failed"
            raise _GitProbeError(diagnostic)
        return Path(root_result.stdout.strip())

    def _fetch_origin(self, root: Path) -> None:
        fetch_result = self._git(root, "fetch", "origin")
        if fetch_result is None or fetch_result.returncode != 0:
            diagnostic = "origin fetch failed"
            raise _GitProbeError(diagnostic, str(root))

    def _head_state(self, root: Path) -> tuple[str, str | None, str]:
        local_result = self._git(root, "rev-parse", "HEAD")
        branch_result = self._git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
        default_result = self._git(
            root,
            "symbolic-ref",
            "--quiet",
            "--short",
            "refs/remotes/origin/HEAD",
        )
        if local_result is None or branch_result is None or default_result is None:
            diagnostic = "git provenance probe failed"
            raise _GitProbeError(diagnostic, str(root))
        local_sha = local_result.stdout.strip()
        branch = branch_result.stdout.strip() or None
        default_branch = self._default_branch(root, default_result)
        if local_result.returncode != 0 or not local_sha or default_branch is None:
            diagnostic = "git provenance probe failed"
            raise _GitProbeError(diagnostic, str(root))
        return local_sha, branch, default_branch

    def _default_comparison(self, root: Path, default_branch: str) -> tuple[str, int, int]:
        remote_ref = f"refs/remotes/origin/{default_branch}"
        default_sha_result = self._git(root, "rev-parse", remote_ref)
        counts_result = self._git(
            root,
            "rev-list",
            "--left-right",
            "--count",
            f"HEAD...{remote_ref}",
        )
        if (
            default_sha_result is None
            or counts_result is None
            or default_sha_result.returncode != 0
            or counts_result.returncode != 0
        ):
            diagnostic = "git provenance probe failed"
            raise _GitProbeError(diagnostic, str(root))
        default_sha = default_sha_result.stdout.strip()
        if not default_sha:
            diagnostic = "git provenance probe failed"
            raise _GitProbeError(diagnostic, str(root))
        ahead_text, behind_text = counts_result.stdout.split()
        return default_sha, int(ahead_text), int(behind_text)

    def _default_branch(self, root: Path, symbolic: CompletedProcess[str]) -> str | None:
        value = symbolic.stdout.strip()
        if symbolic.returncode == 0 and value.startswith("origin/"):
            return value.removeprefix("origin/")
        for name in ("main", "master"):
            result = self._git(root, "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{name}")
            if result is None:
                return None
            if result.returncode == 0:
                return name
        return None

    def _git(self, root: Path, *arguments: str) -> CompletedProcess[str] | None:
        try:
            return self.runner(("git", "-C", str(root), *arguments))
        except SUBPROCESS_UNREACHABLE:
            return None


def _unknown_git(diagnostic: str, *, repo_path: str | None = None) -> GitProvenance:
    return GitProvenance(
        state=GitProbeState.UNKNOWN,
        repo_path=repo_path,
        local_sha=None,
        branch=None,
        detached=None,
        default_branch=None,
        default_sha=None,
        ahead=None,
        behind=None,
        newer_on_default=None,
        diagnostic=diagnostic,
    )
