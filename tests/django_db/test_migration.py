"""Tests for teatree.utils.django_db — reference-DB migration with selective faking.

Split verbatim from the former monolithic ``tests/test_django_db.py``
(souliane/teatree#443). No behavior change.
"""

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from teatree.utils import run as run_mod
from teatree.utils.django_db import _MigrateResult

from ._shared import _make_importer, _ok_run


class TestMigrateReferenceDb:
    def test_succeeds_on_first_try(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(run_mod.subprocess, "run", _ok_run)
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.APPLIED

    def test_returns_already_migrated_when_no_migrations(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(a, 0, "No migrations to apply.\n", ""),
        )
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.ALREADY_MIGRATED

    def test_fakes_already_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        calls: list[list[str]] = []
        call_count = 0

        def fake_run(args, **kw):
            nonlocal call_count
            calls.append(list(args))
            call_count += 1
            if call_count == 1:
                return CompletedProcess(args, 1, "Applying myapp.0005_add_field...\n", "already exists")
            return CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.APPLIED
        assert "--fake" in calls[1]

    def test_skips_on_config_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(a, 1, "", "ModuleNotFoundError: No module named 'foo'"),
        )
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.FAILED

    def test_skips_on_non_fakeable_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(a, 1, "", "unexpected error"),
        )
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.FAILED

    def test_skips_when_no_manage_py(self, tmp_path: Path) -> None:
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.ALREADY_MIGRATED

    def test_skips_when_failing_migration_not_parseable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(a, 1, "Running migrate...\n", "already exists"),
        )
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.FAILED

    def test_exhausts_retries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(a, 1, "Applying myapp.0001_init...\n", "already exists"),
        )
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.FAILED

    def test_fakes_does_not_exist(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        calls: list[list[str]] = []
        call_count = 0

        def fake_run(args, **kw):
            nonlocal call_count
            calls.append(list(args))
            call_count += 1
            if call_count == 1:
                return CompletedProcess(args, 1, "Applying myapp.0005_drop...\n", "does not exist")
            return CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.APPLIED

    def test_subprocess_env_drops_caller_django_settings_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reference-DB migrate must NOT inherit the caller's DJANGO_SETTINGS_MODULE.

        Regression: souliane/teatree#959 — when ``db refresh`` runs from a
        provisioned worktree, the overlay env-cache exports a worktree-specific
        settings module that does not exist in the main clone. Inheriting it
        crashes the migrate subprocess with ``ModuleNotFoundError``, the
        restore pipeline aborts, and the ticket DB is never cloned.
        """
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "worktree_only.settings_local")
        captured_envs: list[dict[str, str]] = []

        def capture_run(args, **kw):
            captured_envs.append(dict(kw.get("env") or {}))
            return CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(run_mod.subprocess, "run", capture_run)
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.APPLIED
        assert captured_envs, "migrate subprocess was not invoked"
        assert "DJANGO_SETTINGS_MODULE" not in captured_envs[0], (
            f"Reference-DB migrate inherited caller's DJANGO_SETTINGS_MODULE: "
            f"{captured_envs[0].get('DJANGO_SETTINGS_MODULE')!r}"
        )

    def test_subprocess_env_keeps_migrate_env_extra_settings_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An overlay-supplied ``migrate_env_extra['DJANGO_SETTINGS_MODULE']`` wins.

        The strip applies only to the inherited caller env, not to an explicit
        overlay override — that's the legitimate way to point the reference-DB
        migrate at a non-default settings module.
        """
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "worktree_only.settings_local")
        captured_envs: list[dict[str, str]] = []

        def capture_run(args, **kw):
            captured_envs.append(dict(kw.get("env") or {}))
            return CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(run_mod.subprocess, "run", capture_run)
        importer = _make_importer(
            tmp_path,
            migrate_env_extra={"DJANGO_SETTINGS_MODULE": "myproj.settings"},
        )
        assert importer._migrate_reference_db() is _MigrateResult.APPLIED
        assert captured_envs[0].get("DJANGO_SETTINGS_MODULE") == "myproj.settings"

    def test_config_error_surfaces_subprocess_output(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed reference-DB migrate must surface the captured stdout/stderr.

        Regression: souliane/teatree#959 — the generic
        ``Cannot migrate reference DB (config error), skipping.`` one-liner
        swallowed the real ``ModuleNotFoundError``, leaving operators with no
        diagnostic to act on. The captured subprocess output is the source of
        truth and must reach the user.
        """
        import io  # noqa: PLC0415

        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(
                a,
                1,
                "Applying initial migration...\n",
                "ModuleNotFoundError: No module named 'worktree_only.settings_local'",
            ),
        )
        importer = _make_importer(tmp_path)
        importer.stdout = io.StringIO()
        importer.stderr = io.StringIO()
        assert importer._migrate_reference_db() is _MigrateResult.FAILED
        combined_output = importer.stdout.getvalue() + importer.stderr.getvalue()
        assert "ModuleNotFoundError" in combined_output, (
            "Reference-DB migrate failure did not surface the real subprocess error; "
            f"captured output was:\nSTDOUT:\n{importer.stdout.getvalue()}\n"
            f"STDERR:\n{importer.stderr.getvalue()}"
        )
        assert "worktree_only.settings_local" in combined_output
