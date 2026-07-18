"""Stateless Postgres primitives for the import engine.

No I/O on ``stdout`` and no per-run state — pure ``psql`` / ``createdb``
plumbing the importer and the snapshot warmer both build on.
"""

import os

from teatree.utils.run import run_allowed_to_fail

#: Host literals that mean "this same host" and therefore never survive the move
#: into a container's network namespace (souliane/teatree#3328). A ``DATABASE_URL``
#: carrying one of these, handed to a dockerized migrate without a rewrite, is the
#: known-bad combination that can silently migrate a *different* Postgres.
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "::1"})


def is_loopback_host(host: str) -> bool:
    """Whether *host* is a loopback literal that is meaningless inside a container."""
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def rewrite_url_host(url: str, host: str) -> str:
    """Return *url* with its network host replaced by *host*, preserving all else.

    User, password, port, scheme, path, query and fragment are kept verbatim —
    only the host component of the authority changes. This is the host→container
    handoff every ``dockerized_migrate`` implementer needs (souliane/teatree#3328):
    a URL built host-side names a host that does not resolve to the intended
    database inside the container's network namespace.

    >>> rewrite_url_host("postgres://u:p%40ss@localhost:5432/db", "postgres-svc")
    'postgres://u:p%40ss@postgres-svc:5432/db'
    >>> rewrite_url_host("postgres://localhost/db", "10.0.0.5")
    'postgres://10.0.0.5/db'
    """
    from urllib.parse import urlsplit, urlunsplit  # noqa: PLC0415 — deferred: only this path needs it

    parts = urlsplit(url)
    userinfo = ""
    if parts.username is not None:
        userinfo = parts.username
        if parts.password is not None:
            userinfo += f":{parts.password}"
        userinfo += "@"
    port = f":{parts.port}" if parts.port is not None else ""
    netloc = f"{userinfo}{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def url_host(url: str) -> str:
    """The host component of *url*, or ``""`` when it carries none."""
    from urllib.parse import urlsplit  # noqa: PLC0415 — deferred: only this path needs it

    return urlsplit(url).hostname or ""


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
