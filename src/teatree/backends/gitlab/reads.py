"""GitLab's read interface, in the shape of :mod:`teatree.backends.github.reads`.

A read takes any client with the GET surface below, so this module imports nothing
that can write. The remaining GitLab reads live in :mod:`teatree.backends.gitlab.pr_reads`.
"""

from typing import Protocol

from teatree.types import RawAPIDict

__all__ = ["list_pr_commits"]


class _ReadClient(Protocol):
    def get_json_paginated(self, endpoint: str) -> list[RawAPIDict]: ...  # pragma: no branch


class _Project(Protocol):
    @property
    def project_id(self) -> int: ...  # pragma: no branch


def list_pr_commits(client: _ReadClient, project: _Project | None, *, pr_iid: int) -> list[RawAPIDict]:
    if project is None:
        return []
    return client.get_json_paginated(f"projects/{project.project_id}/merge_requests/{pr_iid}/commits?per_page=100")
