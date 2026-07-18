"""Bundled overlay for teatree self-development (dogfooding).

Provides a real overlay that exercises the full overlay API using
teatree's own repo, skills, and GitHub project as the target.
"""

from pathlib import Path
from typing import override

from teatree.core.models import Worktree
from teatree.overlay_sdk import (
    OverlayBase,
    OverlayConfig,
    OverlayConnectors,
    OverlayMetadata,
    OverlayProvisioning,
    OverlayReview,
    OverlayRuntime,
    ProvisionStep,
    SkillMetadata,
    clone_root,
    compose_project,
    discover_overlays,
    matches_triggers,
    reap_compose_project,
    run_checked,
)

_SETTINGS_MODULE = "teatree.contrib.t3_teatree.overlay_settings"
_DEFAULT_FOLLOWUP_REPOS = ["souliane/teatree"]


def _is_github_slug(value: str) -> bool:
    owner, sep, name = value.partition("/")
    return bool(sep) and bool(owner) and bool(name) and "/" not in name


def _repo_root() -> Path:
    """Return the teatree repository root (directory containing pyproject.toml)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "skills").is_dir():
            return parent
    msg = f"Cannot find teatree repo root from {here}"
    raise FileNotFoundError(msg)


def _discover_workspace_repos() -> list[str]:
    """Aggregate teatree's own repo + every discovered overlay project path.

    Each path is returned relative to the CLONE root (``config.clone_root()``,
    ``~/workspace``). Overlays whose path lives outside it (or cannot be resolved
    on disk) are skipped — callers can always override via ``config.workspace_repos``.
    """
    workspace_dir = clone_root().resolve()
    candidates: list[Path] = [_repo_root()]
    candidates.extend(entry.project_path for entry in discover_overlays() if entry.project_path is not None)

    seen: set[str] = set()
    repos: list[str] = []
    for candidate in candidates:
        try:
            rel = candidate.resolve().relative_to(workspace_dir)
        except ValueError:
            continue
        key = str(rel)
        if key not in seen:
            seen.add(key)
            repos.append(key)
    return repos


class TeatreeMetadata(OverlayMetadata):
    """Metadata for the bundled teatree overlay."""

    def __init__(self, config: OverlayConfig) -> None:
        self._config = config

    @override
    def get_followup_repos(self) -> list[str]:
        slugs = [repo for repo in self._config.workspace_repos if _is_github_slug(repo)]
        return slugs or list(_DEFAULT_FOLLOWUP_REPOS)

    @override
    def get_skill_metadata(self) -> SkillMetadata:
        root = _repo_root()
        return {
            "skill_path": str(root / "skills"),
            "skill_root": str(root / "skills"),
            "remote_patterns": ["souliane/teatree"],
        }


class TeatreeConnectors(OverlayConnectors):
    @override
    def mcp_provider_expectations(self) -> dict[str, str]:
        # The teatree dogfood overlay declares no per-server provider — the
        # connectivity check (#2282) enforces only connected-ness here. The real
        # per-server values live in the overlay repo (souliane/teatree#251).
        return {}


class TeatreeProvisioning(OverlayProvisioning):
    @override
    def reap_external_resources(self, worktree: Worktree) -> list[str]:
        result = reap_compose_project(compose_project(worktree))
        return [] if result.is_noop else [str(result)]


class TeatreeRuntime(OverlayRuntime):
    @override
    def test_command(self, worktree: Worktree) -> list[str]:
        return ["uv", "run", "pytest"]

    @override
    def lint_command(self, worktree: Worktree) -> list[str]:
        return ["prek", "run", "--all-files"]


class TeatreeReview(OverlayReview):
    @override
    def visual_qa_targets(self, changed_files: list[str]) -> list[str]:
        teatree_globs = (
            "src/teatree/**/templates/**",
            "src/teatree/**/static/**",
            "src/teatree/core/views/**",
            "src/teatree/core/urls.py",
        )
        return ["/"] if matches_triggers(changed_files, teatree_globs) else []

    @override
    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        # Teatree is a developer CLI / agent harness with no customer-facing
        # product surface, so no change ships to a customer display. The
        # mandatory-E2E gate (#1967) is a no-op for this overlay.
        _ = changed_files
        return False


class TeatreeOverlay(OverlayBase):
    """Overlay for developing teatree itself."""

    django_app: str | None = "teatree.contrib.t3_teatree"
    config = OverlayConfig(settings_module=_SETTINGS_MODULE, overlay_name="t3-teatree")
    metadata = TeatreeMetadata(config)
    provisioning = TeatreeProvisioning()
    runtime = TeatreeRuntime()
    review = TeatreeReview()
    connectors = TeatreeConnectors()

    @override
    def get_repos(self) -> list[str]:
        return ["teatree"]

    @override
    def get_checking_sources(self) -> list[str]:
        # The teatree overlay relies on the core needs-you sources (pending
        # questions + failed agent runs); it adds none of its own.
        return []

    @override
    def get_workspace_repos(self) -> list[str]:
        if self.config.workspace_repos:
            return list(self.config.workspace_repos)
        discovered = _discover_workspace_repos()
        return discovered or self.get_repos()

    @override
    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        # ``worktree.repo_path`` is the repo identifier (e.g. ``souliane/teatree``),
        # NOT a filesystem path — the on-disk worktree path lives in ``extra['worktree_path']``
        # and is exposed via ``worktree.worktree_path``. Before #941 this method used
        # ``Path(worktree.repo_path)`` directly, which produced a relative path like
        # ``souliane/teatree`` and caused every ``workspace provision`` to fail with
        # ``FileNotFoundError: 'souliane/teatree'`` on the ``sync-dependencies`` step.
        on_disk = worktree.worktree_path
        if not on_disk:
            # Worktree row exists but has not been materialised on disk yet —
            # ``WorktreeRowProvisionRunner`` populates ``extra['worktree_path']`` after
            # ``git worktree add`` succeeds. Provisioning steps require a real directory,
            # so return an empty list (no-op) rather than crash with a misleading path.
            return []
        repo = Path(on_disk)

        def sync_deps() -> None:
            run_checked(["uv", "sync"], cwd=repo)

        def install_overlays_editable() -> None:
            workspace_dir = clone_root().resolve()
            ticket_dir = repo.parent
            repo_resolved = repo.resolve()
            for entry in discover_overlays():
                if entry.project_path is None:
                    continue
                try:
                    entry.project_path.resolve().relative_to(workspace_dir)
                except ValueError:
                    continue
                overlay_worktree = ticket_dir / entry.project_path.name
                if not overlay_worktree.is_dir():
                    continue
                if overlay_worktree.resolve() == repo_resolved:
                    continue
                run_checked(["uv", "pip", "install", "-e", str(overlay_worktree)], cwd=repo)

        # Both steps are pure subprocess shellouts (uv sync / uv pip install) that
        # touch no ORM, so they are time-boxed on a worker thread (subprocess_only)
        # — a network stall aborts loud instead of hanging the provision silently
        # (souliane/teatree#2244). The teatree overlay declares NO db_import strategy,
        # so these are the ONLY provision steps it runs; without the bound the
        # dogfooding path had no ceiling/heartbeat/alert at all.
        #
        # PR-27 DAG edge: ``install-overlays-editable`` runs ``uv pip install -e``
        # into the venv that ``sync-dependencies`` (``uv sync``) creates, so it
        # ``requires`` the ``python-deps`` token the sync step ``produces`` — the
        # runner then orders them instead of racing them concurrently.
        def python_env_ready() -> bool:
            return (repo / ".venv").is_dir()

        return [
            ProvisionStep(
                name="sync-dependencies",
                callable=sync_deps,
                description="Install Python dependencies with uv sync",
                subprocess_only=True,
                produces=frozenset({"python-deps"}),
                post_condition=python_env_ready,
            ),
            ProvisionStep(
                name="install-overlays-editable",
                callable=install_overlays_editable,
                description="Install discovered overlays editable from their ticket worktrees",
                subprocess_only=True,
                requires=frozenset({"python-deps"}),
            ),
        ]

    @override
    def get_eval_scenarios_dir(self) -> Path | None:
        scenarios = Path(__file__).resolve().parent / "eval" / "scenarios"
        return scenarios if scenarios.is_dir() else None
