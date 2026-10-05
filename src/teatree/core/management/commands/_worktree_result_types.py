"""The structured shapes the ``worktree`` command's leaves return.

Pure data, split out of ``worktree.py`` so the cap-bound command module can take the
``adopt`` mixin without growing (module-health lets an over-cap file only shrink).
``worktree.py`` re-exports every name, so ``from ...commands.worktree import
WorktreeStatus`` keeps resolving for existing consumers.
"""

from typing import TypedDict

from teatree.core.provision.provision_postconditions import PostConditionOutcome


class ProvisionSummary(TypedDict):
    """Rendered summary of the worktree's last ``Worktree.extra['provision_report']``."""

    total_duration: float
    steps: int
    success: bool
    slowest_step: str
    slowest_step_duration: float


class WorktreeStatus(TypedDict, total=False):
    state: str
    repo_path: str
    branch: str
    ports: dict[str, int]
    provision_report: ProvisionSummary
    post_conditions: list[PostConditionOutcome]
    provisioned_ok: bool


class WorktreeDiagnose(TypedDict):
    state: str
    repo_path: str
    worktree_dir: bool
    git_marker: bool
    env_cache: bool
    db_name: str
    docker_services: str


class SmokeCheck(TypedDict, total=False):
    status: str
    detail: str
    repos: list[str]
    worktrees: int
    errors: list[str]


class SmokeReport(TypedDict, total=False):
    overlay: SmokeCheck
    cli: SmokeCheck
    database: SmokeCheck
    hooks: SmokeCheck
    imports: SmokeCheck


class AdoptResult(TypedDict):
    """What ``worktree adopt`` records for an existing on-disk checkout."""

    worktree_id: int
    ticket_id: int
    repo_path: str
    branch: str
    path: str
