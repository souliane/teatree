"""Tests for teatree.utils.django_db — reference-DB migration with selective faking.

Split verbatim from the former monolithic ``tests/test_django_db.py``
(souliane/teatree#443). No behavior change.
"""

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from teatree.utils import run as run_mod
from teatree.utils.django_db.migrate import _MigrateResult

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


class TestMigrateRunnerSelection:
    """The reference-DB migrate must honor the main clone's dependency manager.

    Regression: souliane/teatree#1973 — a Pipfile-based (pipenv) main clone may
    carry only a stub ``uv.lock`` (no ``[[package]]`` entries).
    ``uv --directory <clone> run python`` builds a bare venv from that stub lock,
    so ``import django`` raises ``ModuleNotFoundError``; the migrate is then
    misclassified as a config error, ``_MigrateResult.FAILED`` aborts the
    restore, and the ticket DB is never cloned. The runner prefix must be
    selected from the main clone's dependency manager.
    """

    @staticmethod
    def _capture_migrate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        calls: list[list[str]] = []

        def capture_run(args, **_kw):
            calls.append(list(args))
            return CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(run_mod.subprocess, "run", capture_run)
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.APPLIED
        return calls

    def test_pipfile_with_stub_uv_lock_uses_pipenv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "Pipfile").write_text("[packages]\ndjango = '*'\n", encoding="utf-8")
        (tmp_path / "uv.lock").write_text('version = 1\nrevision = 3\nrequires-python = ">=3.12"\n', encoding="utf-8")
        cmd = self._capture_migrate(tmp_path, monkeypatch)[0]
        assert "uv" not in cmd, f"pipenv main clone must not use `uv run`: {cmd}"
        assert cmd[:2] == ["env", f"PIPENV_PIPFILE={tmp_path / 'Pipfile'}"], cmd
        assert cmd[2:5] == ["pipenv", "run", "python"], cmd
        assert cmd[5:] == ["manage.py", "migrate", "--no-input"], cmd

    def test_pipfile_without_uv_lock_uses_pipenv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "Pipfile").write_text("[packages]\ndjango = '*'\n", encoding="utf-8")
        cmd = self._capture_migrate(tmp_path, monkeypatch)[0]
        assert cmd[:5] == ["env", f"PIPENV_PIPFILE={tmp_path / 'Pipfile'}", "pipenv", "run", "python"], cmd

    def test_real_uv_lock_keeps_uv_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "uv.lock").write_text(
            'version = 1\n\n[[package]]\nname = "django"\nversion = "5.0"\n', encoding="utf-8"
        )
        cmd = self._capture_migrate(tmp_path, monkeypatch)[0]
        assert cmd[:5] == ["uv", "--directory", str(tmp_path), "run", "python"], cmd

    def test_no_lockfile_no_pipfile_keeps_uv_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = self._capture_migrate(tmp_path, monkeypatch)[0]
        assert cmd[:5] == ["uv", "--directory", str(tmp_path), "run", "python"], cmd

    def test_unreadable_uv_lock_treated_as_pipenv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.utils.django_db.runner import _is_pipenv_repo  # noqa: PLC0415

        (tmp_path / "Pipfile").write_text("[packages]\ndjango = '*'\n", encoding="utf-8")
        (tmp_path / "uv.lock").write_text("[[package]]\n", encoding="utf-8")

        def boom(*_a: object, **_kw: object) -> str:
            raise OSError

        monkeypatch.setattr(Path, "read_text", boom)
        assert _is_pipenv_repo(tmp_path) is True

    def test_fake_step_reuses_pipenv_runner(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        (tmp_path / "Pipfile").write_text("[packages]\ndjango = '*'\n", encoding="utf-8")
        calls: list[list[str]] = []
        call_count = 0

        def fake_run(args, **_kw):
            nonlocal call_count
            calls.append(list(args))
            call_count += 1
            if call_count == 1:
                return CompletedProcess(args, 1, "Applying myapp.0005_add_field...\n", "already exists")
            return CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.APPLIED
        assert "--fake" in calls[1]
        assert "uv" not in calls[1], f"fake step must also use pipenv: {calls[1]}"
        assert calls[1][:4] == ["env", f"PIPENV_PIPFILE={tmp_path / 'Pipfile'}", "pipenv", "run"], calls[1]


class TestMigrateDockerizedFallback:
    """An unverified host venv must not be trusted; fall back to the dockerized runner.

    Regression: souliane/teatree#1977 — #1976 routed a Pipfile-based main clone
    through ``pipenv run``, but ``PIPENV_PIPFILE`` pinned to the clone's own
    Pipfile resolves a stale, uv-built in-project ``.venv`` that is missing the
    repo's deps (django may leak in, ``celery`` does not). The host migrate then
    fails with an import error and the ticket DB is never created. When a
    ``dockerized_migrate`` runner is configured, core must attempt+classify: on
    a host config/import error it retries the migrate inside the repo-canonical
    docker image (every dep baked in), so host venv state stops mattering.
    """

    @staticmethod
    def _import_error_run(args, **_kw):
        return CompletedProcess(args, 1, "", "ModuleNotFoundError: No module named 'celery'")

    def test_falls_back_to_docker_on_host_import_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        (tmp_path / "Pipfile").write_text("[packages]\ndjango = '*'\n", encoding="utf-8")
        monkeypatch.setattr(run_mod.subprocess, "run", self._import_error_run)

        docker_calls: list[list[str]] = []

        def dockerized_migrate(manage_args: list[str], _run_env: dict[str, str]) -> CompletedProcess:
            docker_calls.append(list(manage_args))
            return CompletedProcess(manage_args, 0, "Applying myapp.0001_initial... OK\n", "")

        importer = _make_importer(tmp_path, dockerized_migrate=dockerized_migrate)
        assert importer._migrate_reference_db() is _MigrateResult.APPLIED
        assert docker_calls == [["manage.py", "migrate", "--no-input"]], docker_calls

    def test_docker_already_migrated_when_no_migrations(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(run_mod.subprocess, "run", self._import_error_run)

        def dockerized_migrate(manage_args: list[str], _run_env: dict[str, str]) -> CompletedProcess:
            return CompletedProcess(manage_args, 0, "No migrations to apply.\n", "")

        importer = _make_importer(tmp_path, dockerized_migrate=dockerized_migrate)
        assert importer._migrate_reference_db() is _MigrateResult.ALREADY_MIGRATED

    def test_no_docker_fallback_keeps_failed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without a dockerized runner configured, #1976 behaviour is unchanged."""
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        (tmp_path / "Pipfile").write_text("[packages]\ndjango = '*'\n", encoding="utf-8")
        monkeypatch.setattr(run_mod.subprocess, "run", self._import_error_run)
        importer = _make_importer(tmp_path)
        assert importer._migrate_reference_db() is _MigrateResult.FAILED

    def test_docker_fallback_also_fails_returns_failed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(run_mod.subprocess, "run", self._import_error_run)

        def dockerized_migrate(manage_args: list[str], _run_env: dict[str, str]) -> CompletedProcess:
            return CompletedProcess(manage_args, 1, "", "django.db.utils.OperationalError: connection refused")

        importer = _make_importer(tmp_path, dockerized_migrate=dockerized_migrate)
        assert importer._migrate_reference_db() is _MigrateResult.FAILED

    def test_docker_fake_step_uses_docker(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Once switched to docker, the selective --fake step also runs in docker."""
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        monkeypatch.setattr(run_mod.subprocess, "run", self._import_error_run)
        docker_calls: list[list[str]] = []
        call_count = 0

        def dockerized_migrate(manage_args: list[str], _run_env: dict[str, str]) -> CompletedProcess:
            nonlocal call_count
            docker_calls.append(list(manage_args))
            call_count += 1
            if call_count == 1:
                return CompletedProcess(manage_args, 1, "Applying myapp.0005_add_field...\n", "already exists")
            return CompletedProcess(manage_args, 0, "", "")

        importer = _make_importer(tmp_path, dockerized_migrate=dockerized_migrate)
        assert importer._migrate_reference_db() is _MigrateResult.APPLIED
        assert docker_calls[0] == ["manage.py", "migrate", "--no-input"], docker_calls
        assert "--fake" in docker_calls[1], docker_calls


class TestMigrateRenumberReconcile:
    """A master renumber must not block the import; the snapshot record is safely reconciled.

    souliane/teatree#1038: master inserted a migration that bumped later
    numbers, so the DSLR snapshot's old-numbered ``django_migrations`` record
    fails Django's ``check_consistent_history`` BEFORE any forward migrate runs:
    ``realtymodule.0096… is applied before its dependency
    loanrequestmodule.0257_move_participant_authorization_data``. The engine
    must detect the pure renumber, rename the stale record (via the reconcile
    script), and retry the migrate — and must NOT reconcile a genuine
    divergence (real schema drift stays surfaced).
    """

    _INCONSISTENT = (
        "django.db.migrations.exceptions.InconsistentMigrationHistory: "
        "Migration realtymodule.0096_remove_realty_participant_authorization is applied "
        "before its dependency loanrequestmodule.0257_move_participant_authorization_data "
        "on database 'default'."
    )

    def test_reconciles_renumber_then_migrate_succeeds(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.utils.django_db.reconcile import RECONCILE_OK  # noqa: PLC0415

        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        calls: list[list[str]] = []
        call_count = 0

        def fake_run(args, **_kw):
            nonlocal call_count
            calls.append(list(args))
            call_count += 1
            if call_count == 1:
                # First migrate: the renumber trips check_consistent_history.
                return CompletedProcess(args, 1, "", self._INCONSISTENT)
            if call_count == 2:
                # The reconcile script ran and renamed the stale record.
                return CompletedProcess(
                    args,
                    0,
                    f"{RECONCILE_OK} loanrequestmodule.0256_move_participant_authorization_data -> "
                    "loanrequestmodule.0257_move_participant_authorization_data\n",
                    "",
                )
            # Retried migrate now succeeds (history consistent).
            return CompletedProcess(args, 0, "Applying realtymodule.0096... OK\n", "")

        monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.APPLIED
        # Second call must be the reconcile script (manage.py shell -c <script>).
        assert calls[1][-3:-1] == ["shell", "-c"], calls[1]

    def test_passes_dependency_to_reconcile_script(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.utils.django_db.reconcile import RECONCILE_OK  # noqa: PLC0415

        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        captured_envs: list[dict[str, str]] = []
        call_count = 0

        def fake_run(args, **kw):
            nonlocal call_count
            captured_envs.append(dict(kw.get("env") or {}))
            call_count += 1
            if call_count == 1:
                return CompletedProcess(args, 1, "", self._INCONSISTENT)
            if call_count == 2:
                return CompletedProcess(args, 0, f"{RECONCILE_OK} a.b -> a.c\n", "")
            return CompletedProcess(args, 0, "Applying x.0001... OK\n", "")

        monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
        assert _make_importer(tmp_path)._migrate_reference_db() is _MigrateResult.APPLIED
        reconcile_env = captured_envs[1]
        assert reconcile_env["T3_RECONCILE_DEP_APP"] == "loanrequestmodule"
        assert reconcile_env["T3_RECONCILE_DEP_NAME"] == "0257_move_participant_authorization_data"

    def test_does_not_reconcile_genuine_divergence(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.utils.django_db.reconcile import RECONCILE_SKIP  # noqa: PLC0415

        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        calls: list[list[str]] = []
        call_count = 0

        def fake_run(args, **_kw):
            nonlocal call_count
            calls.append(list(args))
            call_count += 1
            if call_count == 1:
                return CompletedProcess(args, 1, "", self._INCONSISTENT)
            # Reconcile script refuses: not a provable renumber.
            return CompletedProcess(args, 0, f"{RECONCILE_SKIP} no-unique-renumber-candidate count=0\n", "")

        monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
        importer = _make_importer(tmp_path)
        assert importer._migrate_reference_db() is _MigrateResult.FAILED
        # The migrate was NOT retried after the SKIP (no further migrate call).
        migrate_calls = [c for c in calls if c[-3:] == ["manage.py", "migrate", "--no-input"]]
        assert len(migrate_calls) == 1, f"divergence must not retry the migrate: {calls}"

    def test_inconsistent_history_with_config_error_is_not_reconciled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A co-occurring import/config error must never trigger the reconcile.

        The reconcile script runs in the same (unverified) venv; acting on it
        would trust deps the migrate could not even import.
        """
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        calls: list[list[str]] = []

        def fake_run(args, **_kw):
            calls.append(list(args))
            return CompletedProcess(args, 1, "", f"{self._INCONSISTENT}\nModuleNotFoundError: No module named 'x'")

        monkeypatch.setattr(run_mod.subprocess, "run", fake_run)
        importer = _make_importer(tmp_path)
        assert importer._migrate_reference_db() is _MigrateResult.FAILED
        assert all("shell" not in c for c in calls), f"reconcile must not run on a config error: {calls}"
