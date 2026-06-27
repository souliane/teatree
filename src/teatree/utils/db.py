import os

from teatree.utils.postgres_secret import resolve_postgres_password
from teatree.utils.run import CommandFailedError, run_allowed_to_fail, run_checked


def pg_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base) if base is not None else os.environ.copy()
    if password := resolve_postgres_password(env):
        env["PGPASSWORD"] = password
    if port := env.get("POSTGRES_PORT", ""):
        env["PGPORT"] = port
    return env


def pg_host() -> str:
    return os.environ.get("POSTGRES_HOST", "localhost")


def pg_user() -> str:
    return os.environ.get("POSTGRES_USER", "postgres")


_TRUNCATION_PATTERNS = ("could not read", "unexpected EOF", "invalid page", "WARNING:  errors ignored")


def _check_truncation(stderr: str, db_name: str, dump_path: str) -> None:
    """Raise if stderr contains signs of a truncated or corrupt dump file."""
    lower = stderr.lower()
    for pattern in _TRUNCATION_PATTERNS:
        if pattern.lower() in lower:
            msg = f"Corrupt or truncated dump detected for {db_name} from {dump_path}: {pattern!r}"
            raise RuntimeError(msg)


def drop_db(db_name: str, *, user: str = "", host: str = "", env: dict[str, str] | None = None) -> None:
    run_checked(
        ["dropdb", "-h", host or pg_host(), "-U", user or pg_user(), "--if-exists", db_name],
        env=env if env is not None else pg_env(),
    )


def db_restore(db_name: str, dump_path: str) -> None:
    env = pg_env()
    host = pg_host()
    user = pg_user()

    run_checked(["dropdb", "-h", host, "-U", user, "--if-exists", db_name], env=env)
    run_checked(["createdb", "-h", host, "-U", user, db_name], env=env)

    inspection = run_allowed_to_fail(["pg_restore", "-l", dump_path], expected_codes=None)
    if inspection.returncode == 0:
        jobs = min(os.cpu_count() or 2, 4)
        cmd = [
            "pg_restore",
            "-h",
            host,
            "-U",
            user,
            "-d",
            db_name,
            "--no-owner",
            "--no-acl",
            f"--jobs={jobs}",
            dump_path,
        ]
        try:
            restore = run_checked(cmd, env=env)
        except CommandFailedError as exc:
            msg = f"pg_restore failed for {db_name} from {dump_path}"
            raise RuntimeError(msg) from exc
        _check_truncation(restore.stderr, db_name, dump_path)
        return

    try:
        restore = run_checked(
            ["psql", "-h", host, "-U", user, "-d", db_name, "-f", dump_path],
            env=env,
        )
    except CommandFailedError as exc:
        msg = f"psql restore failed for {db_name} from {dump_path}"
        raise RuntimeError(msg) from exc
    _check_truncation(restore.stderr, db_name, dump_path)


def db_exists(db_name: str, *, user: str = "", host: str = "", env: dict[str, str] | None = None) -> bool:
    result = run_allowed_to_fail(
        ["psql", "-h", host or pg_host(), "-U", user or pg_user(), "-lqt"],
        env=env if env is not None else pg_env(),
        expected_codes=None,
    )
    return any(line.split("|")[0].strip() == db_name for line in result.stdout.splitlines() if line)
