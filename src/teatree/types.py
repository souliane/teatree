"""Pure data types shared across teatree modules.

These types have no Django dependencies and no imports from ``teatree.core``,
so they can be used by any layer without introducing cycles.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TypedDict


@dataclass(frozen=True)
class RunCommand:
    """Structured run command with explicit working directory.

    Used by ``OverlayBase.get_run_commands()`` to describe how each service
    is launched. Every service comes up via ``docker compose up`` — the
    overlay supplies argv + cwd metadata that other CLI verbs reuse
    (``t3 <overlay> run tests``, ``run backend``, ``run build-frontend``).
    The runner never spawns anything on the host.
    """

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


# ── Sync types (shared vocabulary between core and backends) ─────────

LAST_SYNC_CACHE_KEY = "teatree_followup_last_sync"
PENDING_REVIEWS_CACHE_KEY = "teatree_pending_reviews"

type RawAPIDict = dict[str, object]
type PREntryDict = dict[str, object]


@dataclass(slots=True)
class SyncResult:
    prs_found: int = 0
    issues_found: int = 0
    tickets_created: int = 0
    tickets_updated: int = 0
    labels_fetched: int = 0
    prs_merged: int = 0
    prs_closed: int = 0
    reviews_synced: int = 0
    worktrees_cleaned: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DiscussionSummary:
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(slots=True)
class PREntry:
    url: str
    title: str
    branch: str
    draft: bool
    repo: str
    iid: int
    updated_at: str
    state: str = "opened"
    pipeline_status: str | None = None
    pipeline_url: str | None = None
    approvals: RawAPIDict | None = None
    discussions: list[DiscussionSummary] | None = None
    e2e_test_plan_url: str | None = None
    review_requested: bool | None = None
    reviewer_names: list[str] | None = None
    review_permalink: str | None = None
    review_channel: str | None = None
    notion_status: str | None = None
    notion_url: str | None = None
    draft_comments_pending: bool | None = None
    draft_comments_count: int | None = None
    approvals_dismissed_at: str | None = None
    dismissed_approvers: list[str] | None = None

    def to_dict(self) -> PREntryDict:
        result: PREntryDict = {}
        for k in self.__slots__:
            v = getattr(self, k)
            if v is None:
                continue
            if k == "discussions":
                result[k] = [d.to_dict() for d in v]
            else:
                result[k] = v
        return result


class SyncBackend(ABC):
    @abstractmethod
    def is_configured(self, overlay: object) -> bool: ...

    @abstractmethod
    def sync(self, overlay: object) -> SyncResult: ...
