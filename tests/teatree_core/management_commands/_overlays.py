"""Shared overlay test doubles and import-path constants for management command tests.

Extracted verbatim from the former monolithic ``test_new_management_commands`` module.
"""

import os
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
    OverlayE2E,
    OverlayMetadata,
    OverlayProvisioning,
    OverlayRuntime,
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
    whose ``declared_env_keys`` / ``provisioning.base_images`` return un-iterable
    mocks crashes the renderer. This double pins those to empty collections
    so the env-cache path no-ops cleanly while the test keeps full control
    over the behaviour it actually asserts.
    """
    overlay = MagicMock()
    overlay.provisioning.env_extra.return_value = {}
    overlay.provisioning.declared_env_keys.return_value = set()
    overlay.provisioning.declared_secret_env_keys.return_value = set()
    overlay.provisioning.base_images.return_value = []
    overlay.provisioning.db_import_strategy.return_value = None
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


class FullProvisioning(OverlayProvisioning):
    def db_import_strategy(self, worktree: Worktree) -> DbImportStrategy:
        return {"kind": "test", "source_database": "test_db"}

    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def db_import(  # noqa: PLR0913 — mirrors the OverlayProvisioning.db_import extension-point contract.
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

    def reset_passwords_command(self, worktree: Worktree) -> ProvisionStep | None:
        return ProvisionStep(name="reset-passwords", callable=lambda: None)

    def compose_file(self, worktree: Worktree) -> str:
        return "/fake/docker-compose.yml"


class FullRuntime(OverlayRuntime):
    def run_commands(self, worktree: Worktree) -> RunCommands:
        return {
            "backend": ["echo", "backend", worktree.repo_path],
            "frontend": ["echo", "frontend", worktree.repo_path],
            "build-frontend": ["echo", "build", worktree.repo_path],
        }

    def test_command(self, worktree: Worktree) -> list[str]:
        return ["echo", "tests", worktree.repo_path]

    def lint_command(self, worktree: Worktree) -> list[str]:
        return ["echo", "lint", worktree.repo_path]


class FullE2E(OverlayE2E):
    def env_extras(self, env_cache: dict[str, str]) -> dict[str, str]:
        variant = env_cache.get("WT_VARIANT", "")
        return {"CUSTOMER": variant} if variant else {}


class FullOverlay(OverlayBase):
    metadata = FullMetadata()
    provisioning = FullProvisioning()
    runtime = FullRuntime()
    e2e = FullE2E()

    def get_repos(self) -> list[str]:
        return ["backend", "frontend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []


class _ServicesProvisioning(FullProvisioning):
    def services_config(self, worktree: Worktree) -> dict[str, ServiceSpec]:
        return {
            "postgres": {"start_command": ["echo", "start-pg"]},
            "redis": {},
        }


class ServicesOverlay(FullOverlay):
    """Overlay with services config — used to test _start_services."""

    provisioning = _ServicesProvisioning()


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


class _MinimalRuntime(OverlayRuntime):
    def run_commands(self, worktree: Worktree) -> RunCommands:
        return {}

    def test_command(self, worktree: Worktree) -> list[str]:
        return []


class MinimalOverlay(OverlayBase):
    """Overlay that returns empty/None for most methods — tests fallback paths."""

    metadata = _MinimalMetadata()
    runtime = _MinimalRuntime()

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
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


class _PostDbProvisioning(FullProvisioning):
    def post_db_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return [
            ProvisionStep(name="run-migrations", callable=lambda: None),
            ProvisionStep(name="collectstatic", callable=lambda: None),
        ]


class PostDbStepsOverlay(FullOverlay):
    """Overlay with post-DB steps configured — tests the post-DB loop."""

    provisioning = _PostDbProvisioning()


class _FailingImportProvisioning(FullProvisioning):
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def db_import(  # noqa: PLR0913 — mirrors the OverlayProvisioning.db_import extension-point contract.
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


class FailingImportOverlay(FullOverlay):
    """Overlay where db_import always fails — tests error reporting."""

    provisioning = _FailingImportProvisioning()


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
        self.provisioning = _RemotePathProvisioning()
        self.calls = self.provisioning.calls


class _RemotePathProvisioning(FullProvisioning):
    def __init__(self) -> None:
        self.calls: dict[str, object] = {}

    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def db_import(  # noqa: PLR0913 — mirrors the OverlayProvisioning.db_import extension-point contract.
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


DB_ENV_PROBE = "T3_DB_REFRESH_ENV_BLEED_PROBE"


class _EnvCaptureProvisioning(FullProvisioning):
    """Records the ``os.environ`` state seen during ``db_import`` + reset step."""

    def __init__(self) -> None:
        self.seen: dict[str, object] = {}

    def env_extra(self, worktree: Worktree) -> dict[str, str]:
        return {DB_ENV_PROBE: "applied"}

    def db_import(self, worktree: Worktree, **kwargs: object) -> bool:
        self.seen["import_probe"] = os.environ.get(DB_ENV_PROBE)
        self.seen["import_virtual_env"] = os.environ.get("VIRTUAL_ENV")
        return True

    def reset_passwords_command(self, worktree: Worktree) -> ProvisionStep | None:
        def _capture() -> None:
            self.seen["reset_probe"] = os.environ.get(DB_ENV_PROBE)

        return ProvisionStep(name="reset-passwords", callable=_capture)


class EnvCaptureOverlay(FullOverlay):
    """Overlay recording the env visible during ``db refresh`` — proves no bleed."""

    def __init__(self) -> None:
        super().__init__()
        self.provisioning = _EnvCaptureProvisioning()
        self.seen = self.provisioning.seen


class _PreRunRuntime(FullRuntime):
    def pre_run_steps(self, worktree: Worktree, service: str) -> list[ProvisionStep]:
        def _log_step() -> None:
            extra = dict(worktree.extra or {})
            log = extra.get("pre_run_log", [])
            log.append(service)
            extra["pre_run_log"] = log
            worktree.extra = extra
            worktree.save(update_fields=["extra"])

        return [ProvisionStep(name=f"prep-{service}", callable=_log_step)]


class PreRunOverlay(FullOverlay):
    """Overlay with pre-run steps — tests the pre-run loop in worktree provision."""

    runtime = _PreRunRuntime()


class _ProvenanceE2E(FullE2E):
    def run_provenance(self, spec_path: str) -> str:
        if not spec_path:
            return ""
        return f"{spec_path.rsplit('/', 1)[-1].removesuffix('.spec.ts')}-lane"


class ProvenanceOverlay(FullOverlay):
    """Overlay that resolves a manifest entry id from a spec path (#272).

    Mirrors a real per-spec manifest: the entry id is the spec's CI lane, here
    a simple ``<basename>-lane`` derivation so the test can assert the command
    threads the overlay-resolved id (not just the spec path) into the run
    provenance.
    """

    e2e = _ProvenanceE2E()


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


ENV_CAPTURE_OVERLAY = "tests.teatree_core.management_commands._overlays.EnvCaptureOverlay"


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


class _OverlayRepoMetadata(FullMetadata):
    """An external runner that ships its OWN repo (url + ref) in ``get_e2e_config``.

    The ``url`` is read from ``T3_TEST_OVERLAY_E2E_URL`` so a test can point it at
    a real local upstream under ``tmp_path`` (teatree doctrine: real git, no
    subprocess mocking).
    """

    def get_e2e_config(self) -> dict[str, str]:
        return {
            "runner": "external",
            "project_path": "org-eng/client-workspace",
            "url": os.environ.get("T3_TEST_OVERLAY_E2E_URL", ""),
            "ref": "migration-branch",
            "e2e_dir": "e2e",
        }


class _ExternalRunnerOverlay(FullOverlay):
    metadata = _ExternalRunnerMetadata()


class _PlaywrightArgsE2E(FullE2E):
    def playwright_args(self, spec_path: str) -> list[str]:
        if "api-flow/" in spec_path:
            return ["-c", "api.config.ts"]
        return []


class _PlaywrightArgsOverlay(FullOverlay):
    """External runner that selects a Playwright config per spec lane.

    Mirrors a multi-config Playwright suite (one config per lane): an
    ``api-flow`` spec needs ``-c api.config.ts``, everything else its
    default. Proves the external runner threads the overlay-supplied args
    into the ``npx playwright test`` command.
    """

    metadata = _ExternalRunnerMetadata()
    e2e = _PlaywrightArgsE2E()


class _ProjectRunnerOverlay(FullOverlay):
    metadata = _ProjectRunnerMetadata()


class _InferExternalOverlay(FullOverlay):
    metadata = _InferExternalMetadata()


class _InferProjectOverlay(FullOverlay):
    metadata = _InferProjectMetadata()


class _UnconfiguredOverlay(FullOverlay):
    metadata = _UnconfiguredMetadata()


_EXTERNAL_RUNNER_OVERLAY = "tests.teatree_core.management_commands._overlays._ExternalRunnerOverlay"


_PLAYWRIGHT_ARGS_OVERLAY = "tests.teatree_core.management_commands._overlays._PlaywrightArgsOverlay"


_PROJECT_RUNNER_OVERLAY = "tests.teatree_core.management_commands._overlays._ProjectRunnerOverlay"


_INFER_EXTERNAL_OVERLAY = "tests.teatree_core.management_commands._overlays._InferExternalOverlay"


_INFER_PROJECT_OVERLAY = "tests.teatree_core.management_commands._overlays._InferProjectOverlay"


_UNCONFIGURED_OVERLAY = "tests.teatree_core.management_commands._overlays._UnconfiguredOverlay"


class _OverlayRepoOverlay(FullOverlay):
    metadata = _OverlayRepoMetadata()


_OVERLAY_REPO_OVERLAY = "tests.teatree_core.management_commands._overlays._OverlayRepoOverlay"
