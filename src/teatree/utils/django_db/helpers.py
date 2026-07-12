"""Stateless Postgres primitives for the import engine.

No I/O on ``stdout`` and no per-run state — pure ``psql`` / ``createdb``
plumbing the importer and the snapshot warmer both build on.
"""

import os

from teatree.utils.run import run_allowed_to_fail


def _pg_args() -> tuple[str, str, dict[str, str]]:
    # Deferred so importing the engine facade never eagerly pulls the psycopg-backed
    # ``teatree.utils.db`` stack — only the runner/config surface is wanted by most importers.
    from teatree.utils.db import pg_env, pg_host, pg_user  # noqa: PLC0415 — deferred: call-time import, kept lazy

    return pg_host(), pg_user(), pg_env()


def _local_db_url(db_name: str) -> str:
    from urllib.parse import quote  # noqa: PLC0415 — deferred: loaded only on this code path

    # Deferred (same reason as ``_pg_args``): the DB/secret resolvers stay off the facade import path.
    from teatree.utils.db import pg_host, pg_user  # noqa: PLC0415 — deferred: call-time import, kept lazy
    from teatree.utils.postgres_secret import resolve_postgres_password  # noqa: PLC0415 — deferred: call-time import

    pw = resolve_postgres_password()
    port = os.environ.get("POSTGRES_PORT", "5432")
    return f"postgres://{pg_user()}:{quote(pw, safe='')}@{pg_host()}:{port}/{db_name}"


def _ensure_ref_db(ref_db: str, pg_host: str, pg_user: str, pg_env: dict[str, str]) -> None:
    run_allowed_to_fail(
        ["createdb", "-h", pg_host, "-U", pg_user, ref_db],
        env=pg_env,
        expected_codes=None,
    )


def _terminate_connections(db_name: str, pg_host: str, pg_user: str, pg_env: dict[str, str]) -> None:
    run_allowed_to_fail(
        [
            "psql",
            "-h",
            pg_host,
            "-U",
            pg_user,
            "-d",
            "postgres",
            "-v",
            f"dbname={db_name}",
            "-c",
            (
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :'dbname' AND pid <> pg_backend_pid()"
            ),
        ],
        env=pg_env,
        expected_codes=None,
    )
