"""Canonical ticket repo identity from an overlay's declared project paths."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


@dataclass(slots=True)
class _ProjectPathsRegistry:
    provider: Callable[["Ticket"], Mapping[str, str]] | None = None


_registry = _ProjectPathsRegistry()


def register_project_paths_provider(provider: Callable[["Ticket"], Mapping[str, str]]) -> None:
    _registry.provider = provider


def project_repo_identity_for_ticket(ticket: "Ticket") -> "ProjectRepoIdentity":
    if _registry.provider is None:
        msg = "repo project paths provider is not registered"
        raise RuntimeError(msg)
    return ProjectRepoIdentity(_registry.provider(ticket))


@dataclass(frozen=True, slots=True)
class ProjectRepoIdentity:
    project_paths: Mapping[str, str]

    def canonical(self, repo: str) -> str:
        cleaned = str(repo).strip()
        return self.project_paths.get(cleaned, cleaned) if "/" not in cleaned else cleaned

    def distinct(self, repos: Iterable[str] | None) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for repo in repos or []:
            cleaned = str(repo).strip()
            key = self.canonical(cleaned)
            if key and key not in seen:
                seen.add(key)
                result.append(cleaned)
        return result

    def covers(self, required: Iterable[str], covered: Iterable[str]) -> bool:
        return {self.canonical(repo) for repo in required if str(repo).strip()} <= {
            self.canonical(repo) for repo in covered if str(repo).strip()
        }
