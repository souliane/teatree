from typing import TypedDict

type Ports = dict[str, int]


class TicketExtra(TypedDict, total=False):
    tests_passed: bool
    mr_urls: list[str]


class WorktreeExtra(TypedDict, total=False):
    worktree_path: str
    services: list[str]
    urls: dict[str, str]
    pids: dict[str, int]
    failed_services: list[str]
    db_refreshed_at: str
    setup_hook: str
