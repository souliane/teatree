"""Shared overlay test doubles and import-path constants for management command tests.

Extracted verbatim from the former monolithic ``test_new_management_commands`` module.
"""

import shutil
from unittest.mock import MagicMock, patch

import pytest
from django.utils.module_loading import import_string

import teatree.core.overlay_loader as overlay_loader_mod
from teatree.core.models import Worktree
from teatree.core.overlay import (
    DbImportStrategy,
    OverlayBase,
    OverlayConfig,
    OverlayMetadata,
    ProvisionStep,
    RunCommands,
    ServiceSpec,
    ToolCommand,
    ValidationResult,
)
from teatree.utils.django_db import DjangoDbImportConfig, DjangoDbImporter

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)

_GIT = shutil.which("git") or "git"


def _patch_overlays(overlay_class_path: str):
    """Return a ``patch`` that makes the overlay loader return an instance of *overlay_class_path*.

    Uses ``new`` so the mock is **not** injected as an extra test-method argument.
    The replacement callable carries a no-op ``cache_clear`` so that
    ``reset_overlay_cache()`` keeps working under the patch.
    """
    cls = import_string(overlay_class_path)
    instance = cls()
    result: dict[str, OverlayBase] = {"test": instance}

    def _fake_discover() -> dict[str, OverlayBase]:
        return result

    _fake_discover.cache_clear = lambda: None

    return patch.object(overlay_loader_mod, "_discover_overlays", new=_fake_discover)


def env_safe_mock_overlay() -> MagicMock:
    """A ``MagicMock`` overlay safe to feed through the env-cache renderer.

    The provision/start runners now thread their resolved overlay straight
    into ``write_env_cache`` (souliane/teatree#1975), so a bare ``MagicMock``
    whose ``declared_env_keys`` / ``get_base_images`` return un-iterable
    mocks crashes the renderer. This double pins those to empty collections
    so the env-cache path no-ops cleanly while the test keeps full control
    over the behaviour it actually asserts.
    """
    overlay = MagicMock()
    overlay.get_env_extra.return_value = {}
    overlay.declared_env_keys.return_value = set()
    overlay.declared_secret_env_keys.return_value = set()
    overlay.get_base_images.return_value = []
    overlay.get_db_import_strategy.return_value = None
    return overlay


class FullMetadata(OverlayMetadata):
    def get_ci_project_path(self) -> str:
        return "test/project"

    def detect_variant(self) -> str:
        return "test_variant"

    def get_e2e_config(self) -> dict[str, str]:
        return {"project_path": "test/e2e-project", "ref": "main"}

    def get_tool_commands(self) -> list[ToolCommand]:
        return [
            {"name": "migrate", "help": "Run DB migrations", "command": "echo migrate"},
            {"name": "seed", "help": "Seed test data", "command": "echo seed"},
            {"name": "broken", "help": "No command defined"},
        ]

    def validate_pr(self, title: str, description: str) -> ValidationResult:
        errors = []
        if not title:
            errors.append("Title is required")
        return {"errors": errors, "warnings": []}


class FullOverlay(OverlayBase):
    metadata = FullMetadata()

    def get_repos(self) -> list[str]:
        return ["backend", "frontend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []

    def get_run_commands(self, worktree: Worktree) -> RunCommands:
        return {
            "backend": ["echo", "backend", worktree.repo_path],
            "frontend": ["echo", "frontend", worktree.repo_path],
            "build-frontend": ["echo", "build", worktree.repo_path],
        }

    def get_test_command(self, worktree: Worktree) -> list[str]:
        return ["echo", "tests", worktree.repo_path]

    def get_lint_command(self, worktree: Worktree) -> list[str]:
        return ["echo", "lint", worktree.repo_path]

    def get_db_import_strategy(self, worktree: Worktree) -> DbImportStrategy:
        return {"kind": "test", "source_database": "test_db"}

    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def db_import(  # noqa: PLR0913 — mirrors the OverlayBase.db_import extension-point contract.
        self,
        worktree: Worktree,
        *,
        force: bool = False,
        slow_import: bool = False,
        dslr_snapshot: str = "",
        dump_path: str = "",
        approve_remote_dump: bool = False,
    ) -> bool:
        return True

    def get_reset_passwords_command(self, worktree: Worktree) -> ProvisionStep | None:
        return ProvisionStep(name="reset-passwords", callable=lambda: None)

    def get_compose_file(self, worktree: Worktree) -> str:
        return "/fake/docker-compose.yml"

    def get_e2e_env_extras(self, env_cache: dict[str, str]) -> dict[str, str]:
        variant = env_cache.get("WT_VARIANT", "")
        return {"CUSTOMER": variant} if variant else {}


class ServicesOverlay(FullOverlay):
    """Overlay with services config — used to test _start_services."""

    def get_services_config(self, worktree: Worktree) -> dict[str, ServiceSpec]:
        return {
            "postgres": {"start_command": ["echo", "start-pg"]},
            "redis": {},
        }


class ForbidCloseKeywordsOverlay(FullOverlay):
    """Overlay that manages issue closure via forge linked-items, not trailers.

    Sets ``config.forbid_close_keywords`` so the pre-push close-keyword
    gate (#1012) enforces. ``FullOverlay`` (teatree-style) leaves it at
    the ``False`` default, so a teatree push with ``Closes #N`` is allowed.
    """

    config = OverlayConfig()

    def __init__(self) -> None:
        super().__init__()
        self.config.forbid_close_keywords = True


class CloseTicketOverlay(FullOverlay):
    """teatree-style overlay that auto-closes its issue via ``Closes #N``.

    Sets ``config.mr_close_ticket`` so the closes-issue cross-check gate (#83)
    enforces. ``FullOverlay`` leaves it at the ``False`` default, so the gate
    is a no-op there.
    """

    config = OverlayConfig()

    def __init__(self) -> None:
        super().__init__()
        self.config.mr_close_ticket = True


class _MinimalMetadata(OverlayMetadata):
    def get_tool_commands(self) -> list[ToolCommand]:
        return []


class MinimalOverlay(OverlayBase):
    """Overlay that returns empty/None for most methods — tests fallback paths."""

    metadata = _MinimalMetadata()

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []

    def get_run_commands(self, worktree: Worktree) -> RunCommands:
        return {}

    def get_test_command(self, worktree: Worktree) -> list[str]:
        return []


class _HelplessMetadata(OverlayMetadata):
    def get_tool_commands(self) -> list[ToolCommand]:
        return [{"name": "bare-tool"}]


class HelplessToolOverlay(FullOverlay):
    """Overlay with a tool that has no help text — tests the else branch in list_tools."""

    metadata = _HelplessMetadata()


class NestedRepoOverlay(FullOverlay):
    """Overlay with repos in nested subdirectories of workspace_dir."""

    config = OverlayConfig()

    def __init__(self) -> None:
        super().__init__()
        self.config.workspace_repos = ["org/backend", "org/frontend"]

    def get_repos(self) -> list[str]:
        return ["backend", "frontend"]


class PostDbStepsOverlay(FullOverlay):
    """Overlay with post-DB steps configured — tests the post-DB loop."""

    def get_post_db_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return [
            ProvisionStep(name="run-migrations", callable=lambda: None),
            ProvisionStep(name="collectstatic", callable=lambda: None),
        ]


class FailingImportOverlay(FullOverlay):
    """Overlay where db_import always fails — tests error reporting."""

    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def db_import(  # noqa: PLR0913 — mirrors the OverlayBase.db_import extension-point contract.
        self,
        worktree: Worktree,
        *,
        force: bool = False,
        slow_import: bool = False,
        dslr_snapshot: str = "",
        dump_path: str = "",
        approve_remote_dump: bool = False,
    ) -> bool:
        return False


class RemotePathRecordingOverlay(FullOverlay):
    """Overlay whose ``db_import`` runs a real ``DjangoDbImporter``.

    Used to prove end-to-end (issue #955) that ``db refresh --fresh-dump``
    forwards ``slow_import=True`` so the importer's ``run()`` actually
    reaches the ``allow_remote_dump`` remote-dump branch instead of
    returning early on the ``not slow_import`` / DSLR guard.

    Only the unstoppable subprocess boundaries are mocked: DSLR restore
    (unavailable here so the DSLR fast path is skipped), the remote
    ``pg_dump`` fetch, and the local-dump restore. Everything the fix
    touches — the kwarg threading and ``run()``'s control flow — is real.

    Instances expose ``calls``: ``slow_import`` is the value received by
    ``db_import``; ``remote_branch_reached`` is True iff ``run()`` entered
    the ``if allow_remote_dump:`` block (``_try_fetch_remote_dump`` ran).
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: dict[str, object] = {}

    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def db_import(  # noqa: PLR0913 — mirrors the OverlayBase.db_import extension-point contract.
        self,
        worktree: Worktree,
        *,
        force: bool = False,
        slow_import: bool = False,
        dslr_snapshot: str = "",
        dump_path: str = "",
        approve_remote_dump: bool = False,
    ) -> bool:
        self.calls["slow_import"] = slow_import
        self.calls["approve_remote_dump"] = approve_remote_dump
        self.calls["remote_branch_reached"] = False

        cfg = DjangoDbImportConfig(
            ref_db_name="ref_db",
            ticket_db_name="ticket_db",
            main_repo_path=worktree.repo_path,
            dump_dir="/tmp/does-not-exist",
            dump_glob="*.pgsql",
            ci_dump_glob="*.pgsql",
            remote_db_url="postgres://example/remote",
        )
        importer = DjangoDbImporter(cfg)

        def _fetch_remote() -> bool:
            self.calls["remote_branch_reached"] = True
            return True

        # First local-dump attempt (pre-remote, run() line ~499) finds no
        # local dump → False, so control flows into `if allow_remote_dump:`.
        # The post-fetch restore (run() line ~504) then succeeds.
        local_restore_results = iter([False, True])

        with (
            patch.object(importer, "_try_restore_from_dslr", return_value=False),
            patch.object(
                importer,
                "_try_restore_from_local_dump",
                side_effect=lambda: next(local_restore_results),
            ),
            patch.object(importer, "_try_fetch_remote_dump", side_effect=_fetch_remote),
        ):
            return importer.run(slow_import=slow_import, allow_remote_dump=approve_remote_dump)


class PreRunOverlay(FullOverlay):
    """Overlay with pre-run steps — tests the pre-run loop in worktree provision."""

    def get_pre_run_steps(self, worktree: Worktree, service: str) -> list[ProvisionStep]:
        def _log_step() -> None:
            extra = dict(worktree.extra or {})
            log = extra.get("pre_run_log", [])
            log.append(service)
            extra["pre_run_log"] = log
            worktree.extra = extra
            worktree.save(update_fields=["extra"])

        return [ProvisionStep(name=f"prep-{service}", callable=_log_step)]


class ProvenanceOverlay(FullOverlay):
    """Overlay that resolves a manifest entry id from a spec path (#272).

    Mirrors a real per-spec manifest: the entry id is the spec's CI lane, here
    a simple ``<basename>-lane`` derivation so the test can assert the command
    threads the overlay-resolved id (not just the spec path) into the run
    provenance.
    """

    def get_e2e_run_provenance(self, spec_path: str) -> str:
        if not spec_path:
            return ""
        return f"{spec_path.rsplit('/', 1)[-1].removesuffix('.spec.ts')}-lane"


FULL_OVERLAY = "tests.teatree_core.management_commands._overlays.FullOverlay"


PROVENANCE_OVERLAY = "tests.teatree_core.management_commands._overlays.ProvenanceOverlay"


FORBID_CLOSE_KEYWORDS_OVERLAY = "tests.teatree_core.management_commands._overlays.ForbidCloseKeywordsOverlay"


CLOSE_TICKET_OVERLAY = "tests.teatree_core.management_commands._overlays.CloseTicketOverlay"


NESTED_OVERLAY = "tests.teatree_core.management_commands._overlays.NestedRepoOverlay"


MINIMAL_OVERLAY = "tests.teatree_core.management_commands._overlays.MinimalOverlay"


SERVICES_OVERLAY = "tests.teatree_core.management_commands._overlays.ServicesOverlay"


POST_DB_OVERLAY = "tests.teatree_core.management_commands._overlays.PostDbStepsOverlay"


FAILING_IMPORT_OVERLAY = "tests.teatree_core.management_commands._overlays.FailingImportOverlay"


PRE_RUN_OVERLAY = "tests.teatree_core.management_commands._overlays.PreRunOverlay"


REMOTE_PATH_RECORDING_OVERLAY = "tests.teatree_core.management_commands._overlays.RemotePathRecordingOverlay"


SETTINGS: dict[str, object] = {}


# ── e2e run (harmonized dispatcher) ─────────────────────────────────
#
# Production code reads the runner config via ``overlay.metadata.get_e2e_config()``
# (see ``e2e.py::Command.run``), so each test fixture overrides the metadata
# class — not the overlay class — to change the returned dict.


class _ExternalRunnerMetadata(FullMetadata):
    def get_e2e_config(self) -> dict[str, str]:
        return {"runner": "external", "project_path": "test/e2e-project", "ref": "main"}


class _ProjectRunnerMetadata(FullMetadata):
    def get_e2e_config(self) -> dict[str, str]:
        return {"runner": "project", "test_dir": "e2e/", "settings_module": "e2e.settings"}


class _InferExternalMetadata(FullMetadata):
    def get_e2e_config(self) -> dict[str, str]:
        return {"project_path": "foo/bar", "ref": "main"}


class _InferProjectMetadata(FullMetadata):
    def get_e2e_config(self) -> dict[str, str]:
        return {"test_dir": "e2e/", "settings_module": "e2e.settings"}


class _UnconfiguredMetadata(FullMetadata):
    def get_e2e_config(self) -> dict[str, str]:
        return {}


class _ExternalRunnerOverlay(FullOverlay):
    metadata = _ExternalRunnerMetadata()


class _ProjectRunnerOverlay(FullOverlay):
    metadata = _ProjectRunnerMetadata()


class _InferExternalOverlay(FullOverlay):
    metadata = _InferExternalMetadata()


class _InferProjectOverlay(FullOverlay):
    metadata = _InferProjectMetadata()


class _UnconfiguredOverlay(FullOverlay):
    metadata = _UnconfiguredMetadata()


_EXTERNAL_RUNNER_OVERLAY = "tests.teatree_core.management_commands._overlays._ExternalRunnerOverlay"


_PROJECT_RUNNER_OVERLAY = "tests.teatree_core.management_commands._overlays._ProjectRunnerOverlay"


_INFER_EXTERNAL_OVERLAY = "tests.teatree_core.management_commands._overlays._InferExternalOverlay"


_INFER_PROJECT_OVERLAY = "tests.teatree_core.management_commands._overlays._InferProjectOverlay"


_UNCONFIGURED_OVERLAY = "tests.teatree_core.management_commands._overlays._UnconfiguredOverlay"
