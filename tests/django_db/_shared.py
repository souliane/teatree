"""Shared builders for the django_db test package.

Lifted verbatim from the former monolithic ``tests/test_django_db.py``
(souliane/teatree#443). No behavior change: the same config / importer
factory and the stubbed subprocess results every focused module relies
on, relocated so each split file imports them instead of redefining them.
"""

import io
from pathlib import Path
from subprocess import CompletedProcess

from teatree.utils.django_db import DjangoDbImportConfig, DjangoDbImporter


def _make_cfg(tmp_path: Path, **overrides: str) -> DjangoDbImportConfig:
    defaults = {
        "ref_db_name": "development-acme",
        "ticket_db_name": "wt_42_acme",
        "main_repo_path": str(tmp_path),
        "dump_dir": str(tmp_path / ".data"),
        "dump_glob": "*development-acme*.pgsql",
        "ci_dump_glob": ".gitlab/dump_after_migration.*.sql.gz",
    }
    defaults.update(overrides)
    return DjangoDbImportConfig(**defaults)


def _make_importer(tmp_path: Path, *, dslr_cmd: list[str] | None = None, **cfg_overrides: str) -> DjangoDbImporter:
    """Build an importer with stubbed stdout/stderr and a controllable dslr command."""
    importer = DjangoDbImporter(_make_cfg(tmp_path, **cfg_overrides), stdout=io.StringIO(), stderr=io.StringIO())
    importer.dslr_cmd = dslr_cmd if dslr_cmd is not None else ["/usr/bin/dslr"]
    importer.dslr_env = {"DATABASE_URL": "postgres://u:p@localhost/dev"} if importer.dslr_cmd else {}
    importer.pg_host = "localhost"
    importer.pg_user = "local_superuser"
    importer.pg_env = {"PGPASSWORD": "pw"}
    return importer


def _ok_run(*_args, **_kwargs) -> CompletedProcess:
    return CompletedProcess(args=_args, returncode=0, stdout="", stderr="")


def _fail_run(*_args, **_kwargs) -> CompletedProcess:
    return CompletedProcess(args=_args, returncode=1, stdout="", stderr="error")
