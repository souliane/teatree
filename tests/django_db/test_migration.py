"""Tests for teatree.utils.django_db — reference-DB migration with selective faking.

Split verbatim from the former monolithic ``tests/test_django_db.py``
(souliane/teatree#443). No behavior change.
"""

from pathlib import Path
from subprocess import CompletedProcess

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

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
        from teatree.utils.django_db import _is_pipenv_repo  # noqa: PLC0415

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


class CanonicalizeTeatreeOverlayMigrationTest(TransactionTestCase):
    """0027 collapses the legacy ``teatree`` overlay value to ``t3-teatree``.

    souliane/teatree#1108: the bundled overlay's canonical name is the
    entry-point name ``t3-teatree``. Historical rows written while the
    overlay mislabelled itself ``teatree`` must be canonicalised across
    every overlay-carrying model so discovery/statusline/selectors stop
    treating ``teatree`` and ``t3-teatree`` as distinct overlays. Control
    rows (already-canonical ``t3-teatree`` and empty ``""``) must be
    untouched.
    """

    _BEFORE = ("core", "0026_pending_chat_loop_reply_fields")
    _AFTER = ("core", "0027_canonicalize_teatree_overlay")

    def _migrate(self, target: tuple[str, str]) -> "object":
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        executor.loader.build_graph()
        return executor.loader.project_state([target]).apps

    def _seed_rows(self, apps: "object", overlay: str, tag: str) -> list["object"]:
        """Create one row per overlay-carrying model with *overlay*.

        Returns the created objects so the test can assert how 0027
        rewrites each. ``tag`` keeps unique fields distinct across the
        legacy/control/empty triples without per-row named locals.
        """
        ticket = apps.get_model("core", "Ticket").objects.create(
            overlay=overlay, issue_url=f"https://example.com/issues/{tag}"
        )
        return [
            ticket,
            apps.get_model("core", "Worktree").objects.create(
                overlay=overlay, ticket=ticket, repo_path="teatree", branch=f"b{tag}", db_name=f"d{tag}"
            ),
            apps.get_model("core", "Session").objects.create(overlay=overlay, ticket=ticket),
            apps.get_model("core", "PullRequest").objects.create(
                overlay=overlay, ticket=ticket, url=f"https://example.com/pr/{tag}", repo="teatree", iid=tag
            ),
            apps.get_model("core", "ReviewAssignment").objects.create(
                overlay=overlay,
                mr_url=f"https://example.com/mr/{tag}",
                user_id=f"u{tag}",
                channel=f"c{tag}",
                slack_ts=f"{tag}.1",
                observed_at=timezone.now(),
            ),
            apps.get_model("core", "PendingChatInjection").objects.create(
                overlay=overlay, channel=f"c{tag}", slack_ts=f"{tag}.10", text=f"t{tag}", received_at=timezone.now()
            ),
        ]

    def test_forwards_canonicalizes_only_legacy_teatree(self) -> None:
        apps = self._migrate(self._BEFORE)

        legacy_rows = self._seed_rows(apps, "teatree", "1")
        control_rows = self._seed_rows(apps, "t3-teatree", "2")
        empty_rows = self._seed_rows(apps, "", "3")

        self._migrate(self._AFTER)

        for obj in legacy_rows:
            obj.refresh_from_db()
            assert obj.overlay == "t3-teatree", f"{type(obj).__name__} legacy row not canonicalized"

        for obj in control_rows:
            obj.refresh_from_db()
            assert obj.overlay == "t3-teatree", f"{type(obj).__name__} control row mutated"

        for obj in empty_rows:
            obj.refresh_from_db()
            assert obj.overlay == "", f"{type(obj).__name__} empty-overlay row mutated"

        # Restore the schema to the latest state so TransactionTestCase
        # teardown's flush targets the real (head) table set and downstream
        # tests (e.g. ``test_schema_guard``) see a clean ledger. Resolve the
        # leaf from the live graph so new migrations don't leave 0028+
        # unrecorded for the rest of the session.
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        core_leaves = [node for node in executor.loader.graph.leaf_nodes() if node[0] == "core"]
        executor.migrate(core_leaves)
