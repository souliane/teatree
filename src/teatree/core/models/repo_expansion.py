from typing import TypedDict


class RepoExpansion(TypedDict):
    repos: list[str]
    reason: str


class TicketRepoExtra(TypedDict, total=False):
    branches: dict[str, str]
    repo_expansions: list[RepoExpansion]
