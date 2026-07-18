"""Container-ready migrate env: URL host rewrite + the loopback warning (souliane/teatree#3328).

The dockerized migrate seam builds ``DATABASE_URL`` host-side, where the host is
meaningless inside the container's network namespace. These tests pin the rewrite
helper and the importer's dispatch: the env handed to a fake ``dockerized_migrate``
carries the REWRITTEN host, while the host-side runner keeps the original.
"""

import dataclasses
import io
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest

from teatree.utils import run as run_mod
from teatree.utils.django_db import DjangoDbImportConfig, DjangoDbImporter
from teatree.utils.django_db.helpers import is_loopback_host, rewrite_url_host, url_host

_LOOPBACK_URL = "postgres://u:p@localhost:5432/db"


def _make_importer(tmp_path: Path, **cfg_overrides: Any) -> DjangoDbImporter:
    cfg = DjangoDbImportConfig(
        ref_db_name="development-acme",
        ticket_db_name="wt_42_acme",
        main_repo_path=str(tmp_path),
        dump_dir=str(tmp_path / ".data"),
        dump_glob="*development-acme*.pgsql",
        ci_dump_glob=".gitlab/dump_after_migration.*.sql.gz",
        **cfg_overrides,
    )
    return DjangoDbImporter(cfg, stdout=io.StringIO(), stderr=io.StringIO())


def _capture_env(importer: DjangoDbImporter, sink: dict[str, str]) -> DjangoDbImporter:
    """Rebind the importer's dockerized_migrate to one that records the env it is handed."""
    importer.cfg = dataclasses.replace(
        importer.cfg, dockerized_migrate=lambda _a, env: sink.update(env) or CompletedProcess([], 0)
    )
    importer._migrate_via_docker = True
    return importer


class TestRewriteUrlHost:
    def test_replaces_only_the_host(self) -> None:
        assert rewrite_url_host(_LOOPBACK_URL, "postgres-svc") == "postgres://u:p@postgres-svc:5432/db"

    def test_preserves_encoded_password_user_port_and_path(self) -> None:
        out = rewrite_url_host("postgres://user:p%40ss@127.0.0.1:6543/db?sslmode=require", "10.0.0.5")
        assert out == "postgres://user:p%40ss@10.0.0.5:6543/db?sslmode=require"

    def test_rewrites_hostless_authority(self) -> None:
        assert rewrite_url_host("postgres://localhost/db", "db-host") == "postgres://db-host/db"


class TestUrlHost:
    def test_extracts_host(self) -> None:
        assert url_host("postgres://u:p@db.internal:5432/x") == "db.internal"

    def test_empty_when_absent(self) -> None:
        assert url_host("") == ""


class TestIsLoopbackHost:
    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.1.2.3", "::1"])
    def test_loopback_literals(self, host: str) -> None:
        assert is_loopback_host(host)

    @pytest.mark.parametrize("host", ["db", "postgres-svc", "10.0.0.5", "example.com"])
    def test_routable_hosts(self, host: str) -> None:
        assert not is_loopback_host(host)


class TestContainerizedEnv:
    """``_run_migrate`` dispatch: the container gets a container-oriented env."""

    def test_rewrites_database_url_host_for_the_container(self, tmp_path: Path) -> None:
        seen: dict[str, str] = {}
        importer = _capture_env(_make_importer(tmp_path, docker_db_host="postgres-svc"), seen)
        importer._run_migrate(["manage.py", "migrate"], {"DATABASE_URL": _LOOPBACK_URL})
        assert seen["DATABASE_URL"] == "postgres://u:p@postgres-svc:5432/db"

    def test_host_side_runner_keeps_original_host(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Anti-vacuity: only the container env is rewritten; the host-side runner
        # (docker OFF) must still receive the ORIGINAL host — a test that merely
        # checked "a URL is present" would pass against the buggy pre-#3328 code.
        captured: dict[str, str] = {}
        monkeypatch.setattr(
            run_mod.subprocess,
            "run",
            lambda _args, **kw: captured.update(kw["env"]) or CompletedProcess([], 0, "", ""),
        )
        importer = _make_importer(tmp_path, docker_db_host="postgres-svc")
        importer._migrate_via_docker = False
        importer._run_migrate(["manage.py", "migrate"], {"DATABASE_URL": _LOOPBACK_URL})
        assert captured["DATABASE_URL"] == _LOOPBACK_URL

    def test_empty_docker_host_leaves_url_untouched(self, tmp_path: Path) -> None:
        seen: dict[str, str] = {}
        importer = _capture_env(_make_importer(tmp_path), seen)
        importer._run_migrate(["manage.py", "migrate"], {"DATABASE_URL": "postgres://u:p@remote-db:5432/db"})
        assert seen["DATABASE_URL"] == "postgres://u:p@remote-db:5432/db"

    def test_warns_on_loopback_host_without_docker_db_host(self, tmp_path: Path) -> None:
        importer = _capture_env(_make_importer(tmp_path), {})
        importer.stderr = io.StringIO()
        importer._run_migrate(["manage.py", "migrate"], {"DATABASE_URL": _LOOPBACK_URL})
        warning = importer.stderr.getvalue()
        assert "loopback" in warning
        assert "docker_db_host" in warning

    def test_no_warning_when_docker_db_host_set(self, tmp_path: Path) -> None:
        importer = _capture_env(_make_importer(tmp_path, docker_db_host="postgres-svc"), {})
        importer.stderr = io.StringIO()
        importer._run_migrate(["manage.py", "migrate"], {"DATABASE_URL": _LOOPBACK_URL})
        assert importer.stderr.getvalue() == ""
