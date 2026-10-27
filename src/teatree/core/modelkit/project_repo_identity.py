"""Canonical ticket repo identity from an overlay's declared project paths."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from teatree.core.modelkit.gate_registry import get_resolver

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


def project_repo_identity_for_ticket(ticket: "Ticket") -> "ProjectRepoIdentity":
    return ProjectRepoIdentity(cast("Mapping[str, str]", get_resolver("repo_project_paths")(ticket)))


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

    def covers(self, required: Iterable[str] | None, covered: Iterable[str] | None) -> bool:
        return {self.canonical(repo) for repo in required or [] if str(repo).strip()} <= {
            self.canonical(repo) for repo in covered or [] if str(repo).strip()
        }
