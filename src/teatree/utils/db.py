import os
import subprocess


def pg_env() -> dict[str, str]:
    env = os.environ.copy()
    if password := os.environ.get("POSTGRES_PASSWORD", ""):
        env["PGPASSWORD"] = password
    if port := os.environ.get("POSTGRES_PORT", ""):
        env["PGPORT"] = port
    return env


def pg_host() -> str:
    return os.environ.get("POSTGRES_HOST", "localhost")


def pg_user() -> str:
    return os.environ.get("POSTGRES_USER", "postgres")


def worktree_db_name(ticket_number: str, variant: str) -> str:
    suffix = f"_{variant}" if variant else ""
    return f"wt_{ticket_number}{suffix}"


_TRUNCATION_PATTERNS = ("could not read", "unexpected EOF", "invalid page", "WARNING:  errors ignored")


def _check_truncation(stderr: str, db_name: str, dump_path: str) -> None:
    """Raise if stderr contains signs of a truncated or corrupt dump file."""
    lower = stderr.lower()
    for pattern in _TRUNCATION_PATTERNS:
        if pattern.lower() in lower:
            msg = f"Corrupt or truncated dump detected for {db_name} from {dump_path}: {pattern!r}"
            raise RuntimeError(msg)


def db_restore(db_name: str, dump_path: str) -> None:
    env = pg_env()
    host = pg_host()
    user = pg_user()

    subprocess.run(["dropdb", "-h", host, "-U", user, "--if-exists", db_name], env=env, check=False)
    subprocess.run(["createdb", "-h", host, "-U", user, db_name], env=env, check=True)

    inspection = subprocess.run(["pg_restore", "-l", dump_path], capture_output=True, text=True, check=False)
    if inspection.returncode == 0:
        restore = subprocess.run(
            ["pg_restore", "-h", host, "-U", user, "-d", db_name, "--no-owner", "--no-acl", dump_path],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if restore.returncode != 0:
            msg = f"pg_restore failed for {db_name} from {dump_path}"
            raise RuntimeError(msg)
        _check_truncation(restore.stderr, db_name, dump_path)
        return

    restore = subprocess.run(
        ["psql", "-h", host, "-U", user, "-d", db_name, "-f", dump_path],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if restore.returncode != 0:
        msg = f"psql restore failed for {db_name} from {dump_path}"
        raise RuntimeError(msg)
    _check_truncation(restore.stderr, db_name, dump_path)


def db_exists(db_name: str) -> bool:
    result = subprocess.run(
        ["psql", "-h", pg_host(), "-U", pg_user(), "-lqt"],
        env=pg_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    return any(line.split("|")[0].strip() == db_name for line in result.stdout.splitlines() if line)
