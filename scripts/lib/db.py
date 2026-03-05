"""Database helpers for Postgres."""

import os
import subprocess


def pg_env() -> dict[str, str]:
    """Build env dict with PGPASSWORD and PGPORT set."""
    env = os.environ.copy()
    pw = os.environ.get("POSTGRES_PASSWORD", "")
    if pw:
        env["PGPASSWORD"] = pw
    port = os.environ.get("POSTGRES_PORT", "")
    if port:
        env["PGPORT"] = port
    return env


def pg_host() -> str:
    return os.environ.get("POSTGRES_HOST", "localhost")


def pg_user() -> str:
    return os.environ.get("POSTGRES_USER", "postgres")


def worktree_db_name(ticket_number: str, variant: str) -> str:
    """Build the worktree DB name from ticket number and optional variant."""
    db_name = f"wt_{ticket_number}"
    if variant:
        db_name += f"_{variant}"
    return db_name


def db_restore(db_name: str, dump_path: str) -> None:
    """Drop, create, and restore a Postgres database from a dump file."""
    env = pg_env()
    host = pg_host()
    user = pg_user()

    subprocess.run(
        ["dropdb", "-h", host, "-U", user, "--if-exists", db_name],
        env=env,
        check=False,
    )
    subprocess.run(["createdb", "-h", host, "-U", user, db_name], env=env, check=True)

    # Detect format: try pg_restore -l first.
    result = subprocess.run(["pg_restore", "-l", dump_path], capture_output=True, text=True)
    if result.returncode == 0:
        # Check if the TOC listing itself reveals truncation
        if "could not read" in (result.stderr or ""):
            msg = f"Dump file appears truncated: {dump_path}"
            raise RuntimeError(msg)
        restore = subprocess.run(
            [
                "pg_restore",
                "-h",
                host,
                "-U",
                user,
                "-d",
                db_name,
                "--no-owner",
                "--no-acl",
                dump_path,
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        stderr = restore.stderr or ""
        if restore.returncode != 0 or "could not read" in stderr:
            detail = stderr.strip().splitlines()[-1] if stderr.strip() else ""
            msg = f"pg_restore failed for {db_name} from {dump_path}"
            if detail:
                msg += f": {detail}"
            raise RuntimeError(msg)
    else:
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


def db_exists(db_name: str) -> bool:
    """Check if a Postgres database exists."""
    env = pg_env()
    result = subprocess.run(
        ["psql", "-h", pg_host(), "-U", pg_user(), "-lqt"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        fields = line.split("|")
        if fields and fields[0].strip() == db_name:
            return True
    return False
