"""Environment, path detection, and worktree context helpers."""

import fcntl
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from lib.ports import port_in_use

_MAX_SCAN_DEPTH = 2
_SHARED_REDIS_PORT = 6379
_DEFAULT_POSTGRES_PORT = 5432


_TRUTHY_VALUES = {"true", "1", "yes"}


def share_db_server() -> bool:
    """Return True when worktrees should share a single postgres server.

    Controlled by T3_SHARE_DB_SERVER (default: true). When enabled,
    ``find_free_ports`` reuses the postgres port from the first existing
    worktree (or the default 5432) instead of allocating a new one per
    worktree. Each worktree still gets its own database name.
    """
    return os.environ.get("T3_SHARE_DB_SERVER", "true").lower() in _TRUTHY_VALUES


def _detect_shared_postgres_port(exclude_dir: str = "") -> int:
    """Find the postgres port already in use by another worktree.

    Scans .env.worktree files for POSTGRES_PORT. Returns the first found,
    or _DEFAULT_POSTGRES_PORT if none exist (first worktree scenario).
    """
    ws = workspace_dir()
    for root, dirs, files in os.walk(ws):
        depth = root[len(ws) :].count(os.sep)
        if depth > _MAX_SCAN_DEPTH:
            dirs.clear()
            continue
        if ".env.worktree" not in files:
            continue
        envwt = Path(root) / ".env.worktree"
        if envwt.is_symlink():
            continue
        if exclude_dir and str(envwt).startswith(exclude_dir + "/"):
            continue
        try:
            port = read_env_key(str(envwt), "POSTGRES_PORT")
        except OSError:
            continue
        try:
            parsed = int(port) if port else 0
        except ValueError:
            parsed = 0
        if parsed:
            return parsed
    return _DEFAULT_POSTGRES_PORT


def load_env_worktree() -> None:
    """Load .env.worktree into os.environ (KEY=VALUE lines, no shell expansion).

    Searches upward from CWD for .env.worktree, then loads all KEY=VALUE lines
    into the process environment. Existing env vars are NOT overwritten.
    """
    cwd = Path(_effective_cwd())
    for parent in [cwd, *cwd.parents]:
        envfile = parent / ".env.worktree"
        if envfile.is_file():
            with envfile.open(encoding="utf-8") as f:
                for raw_line in f:
                    stripped = raw_line.strip()
                    if not stripped or stripped.startswith("#") or "=" not in stripped:
                        continue
                    key, _, value = stripped.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())
            return


_SKILL_ROOTS = (
    "~/.agents/skills",
    "~/.claude/skills",
    "~/.codex/skills",
    "~/.cursor/skills",
    "~/.copilot/skills",
)


def skill_dirs() -> list[tuple[Path, str]]:
    """Yield (resolved_path, skill_name) for every installed skill directory."""
    results: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for root_pattern in _SKILL_ROOTS:
        root = Path(root_pattern).expanduser()
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() and not entry.is_symlink():
                continue
            name = entry.name
            if name in seen:
                continue
            seen.add(name)
            resolved = entry.resolve()
            if resolved.is_dir():
                results.append((resolved, name))
    return results


def _effective_cwd() -> str:
    """Return the caller's original working directory.

    When invoked via _wt_python (bootstrap.sh), CWD is the scripts dir.
    The original CWD is preserved in $_T3_ORIG_CWD.
    """
    return os.environ.get("_T3_ORIG_CWD") or str(Path.cwd())


def workspace_dir() -> str:
    return os.environ.get("T3_WORKSPACE_DIR", str(Path("~/workspace").expanduser()))


def branch_prefix() -> str:
    prefix = os.environ.get("T3_BRANCH_PREFIX")
    if prefix:
        return prefix
    try:
        result = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True,
            text=True,
            check=True,
        )
        name = result.stdout.strip()
        if name:
            return "".join(word[0].lower() for word in name.split() if word)
    except (subprocess.CalledProcessError, IndexError):
        pass
    return "wt"


def detect_ticket_dir() -> str:
    """Detect the ticket directory.

    Priority: $TICKET_DIR env > $PWD heuristic.
    """
    td = os.environ.get("TICKET_DIR", "")
    if td and Path(td).is_dir():
        return td

    cwd = _effective_cwd()
    ws = workspace_dir()
    if not cwd.startswith(ws + "/"):
        return ""

    rel = cwd[len(ws) + 1 :]
    parts = rel.split("/", 1)

    candidate = str(Path(ws) / parts[0])
    # It's a ticket dir if it's not a main repo (no .git directory)
    if not (Path(candidate) / ".git").is_dir():
        return candidate
    return ""


@dataclass
class WorktreeContext:
    wt_dir: str
    ticket_dir: str
    ticket_number: str
    main_repo: str
    repo_name: str


def resolve_context(repo: str = "") -> WorktreeContext:
    """Resolve worktree context from $PWD or $TICKET_DIR.

    Works from two locations:
    1. Inside a repo worktree: ~/workspace/<ticket-dir>/<repo>/
    2. Inside the ticket dir:  ~/workspace/<ticket-dir>/

    In case 2, auto-detects the first repo subdirectory whose name matches
    a main repo (~/workspace/<name>/.git exists), or uses the explicit `repo`
    parameter if provided.

    Raises RuntimeError if context cannot be resolved.
    """
    cwd = _effective_cwd()
    ws = workspace_dir()
    ticket_dir = detect_ticket_dir()

    if ticket_dir and cwd == ticket_dir:
        # CWD is the ticket dir itself — find a repo inside it
        repo_name = repo
        if not repo_name:
            for child in sorted(Path(ticket_dir).iterdir()):
                if child.is_dir() and (Path(ws) / child.name / ".git").is_dir():
                    repo_name = child.name
                    break
        if not repo_name:
            msg = f"No repo found in ticket dir {ticket_dir}. Run from inside a repo worktree or pass repo= explicitly."
            raise RuntimeError(msg)
        wt_dir = str(Path(ticket_dir) / repo_name)
    elif ticket_dir:
        # CWD is inside a repo under the ticket dir
        repo_name = repo
        if not repo_name:
            rel = os.path.relpath(cwd, ticket_dir)
            first = rel.split(os.sep, 1)[0]
            repo_name = first if first and first != "." else Path(cwd).name
        wt_dir = str(Path(ticket_dir) / repo_name)
    else:
        msg = "Not in a worktree (not under $T3_WORKSPACE_DIR/<ticket-dir>/). Run from a ticket dir or repo worktree."
        raise RuntimeError(msg)

    main_repo = str(Path(ws) / repo_name)
    if not (Path(main_repo) / ".git").is_dir():
        msg = f"Main repo not found at {main_repo}"
        raise RuntimeError(msg)

    dir_base = Path(ticket_dir).name
    match = re.search(r"\d+", dir_base)
    if not match:
        msg = f"Could not extract ticket number from {dir_base}"
        raise RuntimeError(msg)

    return WorktreeContext(
        wt_dir=wt_dir,
        ticket_dir=ticket_dir,
        ticket_number=match.group(),
        main_repo=main_repo,
        repo_name=repo_name,
    )


def _parse_port_line(line: str, prefix: str) -> int | None:
    if not line.startswith(prefix):
        return None
    try:
        return int(line.split("=", 1)[1])
    except ValueError:
        return None


def _collect_used_ports(
    envwt: Path,
    used_be: set[int],
    used_fe: set[int],
    used_pg: set[int],
    used_rd: set[int],
) -> None:
    try:
        with Path(envwt).open(encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                value = _parse_port_line(line, "BACKEND_PORT=") or _parse_port_line(
                    line,
                    "DJANGO_RUNSERVER_PORT=",
                )
                if value is not None:
                    used_be.add(value)
                    continue
                value = _parse_port_line(line, "FRONTEND_PORT=")
                if value is not None:
                    used_fe.add(value)
                    continue
                value = _parse_port_line(line, "POSTGRES_PORT=")
                if value is not None:
                    used_pg.add(value)
                    continue
                value = _parse_port_line(line, "REDIS_PORT=")
                if value is not None:
                    used_rd.add(value)
    except OSError:
        pass


def _next_free_port(start: int, used: set[int], *, check_system: bool = True) -> int:
    """Find the next port not in *used* and (optionally) not bound on the system."""
    port = start
    while port in used or (check_system and port_in_use(port)):
        used.add(port)  # avoid rechecking
        port += 1
    return port


def _find_free_ports_unlocked(
    exclude_dir: str = "",
    *,
    check_system: bool = True,
) -> tuple[int, int, int, int]:
    """Core port allocation logic (caller must hold the lock)."""
    ws = workspace_dir()
    used_be: set[int] = set()
    used_fe: set[int] = set()
    used_pg: set[int] = set()
    used_rd: set[int] = set()

    for root, dirs, files in os.walk(ws):
        # Go up to _MAX_SCAN_DEPTH levels deep (ticket-dir/repo/.env.worktree)
        depth = root[len(ws) :].count(os.sep)
        if depth > _MAX_SCAN_DEPTH:
            dirs.clear()
            continue
        if ".env.worktree" not in files:
            continue
        envwt = Path(root) / ".env.worktree"
        if envwt.is_symlink():
            continue
        if exclude_dir and str(envwt).startswith(exclude_dir + "/"):
            continue
        _collect_used_ports(envwt, used_be, used_fe, used_pg, used_rd)

    backend_port = _next_free_port(8001, used_be, check_system=check_system)
    frontend_port = _next_free_port(4201, used_fe, check_system=check_system)

    if share_db_server():
        postgres_port = _detect_shared_postgres_port(exclude_dir)
    else:
        postgres_port = _next_free_port(5433, used_pg, check_system=check_system)

    return backend_port, frontend_port, postgres_port, _SHARED_REDIS_PORT


def find_free_ports(
    exclude_dir: str = "",
    *,
    check_system: bool = True,
) -> tuple[int, int, int, int]:
    """Find next available backend/frontend/postgres port triple + shared Redis.

    Scans .env.worktree files in $T3_WORKSPACE_DIR and (when *check_system* is
    True) also verifies that the candidate port is not already bound on the
    host.  Returns (backend_port, frontend_port, postgres_port, redis_port).

    Uses file-based locking to prevent race conditions when multiple
    ``wt_setup`` processes run concurrently.

    Postgres ports start at 5433 for worktrees (5432 is reserved for the
    main repo / host PostgreSQL).  Redis is shared across all worktrees on
    port 6379 — no per-worktree isolation needed (queues are separated by
    ``CELERY_QUEUE_NAME``, cache keys are scoped by tenant/DB context).
    """
    ws = workspace_dir()
    lockfile = Path(ws) / ".port-allocation.lock"
    lockfile.parent.mkdir(parents=True, exist_ok=True)

    fd = lockfile.open("w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _find_free_ports_unlocked(exclude_dir, check_system=check_system)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def _port_held_by_worktree(port: int) -> bool:
    """Return True if the process listening on *port* belongs to this worktree.

    A "worktree process" is one whose command line contains the ticket dir path
    or is a known service (postgres, redis, node/nx) started for this worktree.
    When the port is held by our own process, ``revalidate_ports`` should NOT
    reallocate — the service is intentionally running.
    """
    ticket_dir = os.environ.get("TICKET_DIR", "")
    if not ticket_dir:
        return False
    result = subprocess.run(
        ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
    )
    if not result.stdout.strip():
        return False
    for pid in result.stdout.strip().split("\n"):
        # Read the command line of the process
        ps_result = subprocess.run(
            ["ps", "-p", pid, "-o", "command="],
            capture_output=True,
            text=True,
        )
        cmdline = ps_result.stdout.strip()
        if ticket_dir in cmdline:
            return True
        # Also match postgres/redis containers that serve this worktree
        wt_dir = os.environ.get("WT_DIR", "")
        if wt_dir and wt_dir in cmdline:
            return True
    return False


def revalidate_ports() -> dict[str, int] | None:
    """Check if the ports in the current env are actually free on the system.

    If any port is in use **by a foreign process**, reallocate ALL ports via
    ``find_free_ports`` and rewrite ``$TICKET_DIR/.env.worktree``.

    Ports held by the current worktree's own processes (backend, frontend,
    postgres) are considered expected and are NOT reallocated.

    Returns a dict of updated env-var names → new values, or *None* if no
    changes were needed.
    """
    port_keys = {
        "BACKEND_PORT": int(os.environ.get("BACKEND_PORT") or os.environ.get("DJANGO_RUNSERVER_PORT") or "0"),
        "FRONTEND_PORT": int(os.environ.get("FRONTEND_PORT") or "0"),
        "POSTGRES_PORT": int(os.environ.get("POSTGRES_PORT") or "0"),
    }

    conflicts = {k: v for k, v in port_keys.items() if v and port_in_use(v) and not _port_held_by_worktree(v)}
    if not conflicts:
        return None

    print("--- Port re-validation ---")
    for k, v in conflicts.items():
        print(f"  CONFLICT: {k}={v} is already in use")

    ticket_dir = detect_ticket_dir()
    if not ticket_dir:
        print("  WARNING: Cannot reallocate — no ticket dir found")
        return None

    be, fe, pg, _rd = find_free_ports(ticket_dir)
    updates = {
        "BACKEND_PORT": be,
        "FRONTEND_PORT": fe,
        "POSTGRES_PORT": pg,
        "DJANGO_RUNSERVER_PORT": be,
        "BACK_END_URL": f"http://localhost:{be}",
        "FRONT_END_URL": f"http://localhost:{fe}",
        "CORS_WHITE_FRONT": f"http://localhost:{fe},http://localhost:4800",
    }
    # Also update DATABASE_URL if present
    pg_user = os.environ.get("POSTGRES_USER", "local_superuser")
    pg_pass = os.environ.get("POSTGRES_PASSWORD", "local_superpassword")
    db_name = os.environ.get("POSTGRES_DB") or os.environ.get("WT_DB_NAME", "")
    if db_name:
        updates["DATABASE_URL"] = f"postgresql://{pg_user}:{pg_pass}@localhost:{pg}/{db_name}"

    # Rewrite .env.worktree
    envfile = Path(ticket_dir) / ".env.worktree"
    if envfile.is_file():
        _rewrite_env_worktree(str(envfile), updates)

    # Export into current process
    for k, v in updates.items():
        os.environ[k] = str(v)

    print(f"  Reallocated: BE={be} FE={fe} PG={pg}")
    return {k: v for k, v in updates.items() if isinstance(v, int)}


def _rewrite_env_worktree(filepath: str, updates: dict[str, object]) -> None:
    """Rewrite an .env.worktree file, replacing matching KEY=... lines."""
    lines: list[str] = []
    try:
        with Path(filepath).open(encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return

    new_lines: list[str] = []
    written_keys: set[str] = set()
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}\n")
            written_keys.add(key)
        else:
            new_lines.append(line)

    with Path(filepath).open("w", encoding="utf-8") as f:
        f.writelines(new_lines)


def resolve_repo_dir(repo: str, *, strict: bool = True) -> str:
    """Resolve a repo directory path: worktree > main workspace.

    When ``strict=True`` (default) and we're in a ticket context, raises
    ``RuntimeError`` if the repo worktree is missing.  Use ``strict=False``
    for repos that can safely fall back to the main workspace (e.g.
    read-only data repos like shared-translations).
    """
    ws = workspace_dir()
    td = detect_ticket_dir()
    if td:
        candidate = Path(td) / repo
        if candidate.is_dir():
            return str(candidate)
        if strict:
            msg = f"{repo} worktree not found in {td}. Add it with: t3_ticket <ticket> <desc> {repo}"
            raise RuntimeError(msg)
    return str(Path(ws) / repo)


def read_env_key(filepath: str, key: str) -> str:
    """Read a single key=value from an env file."""
    try:
        with Path(filepath).open(encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1]
    except FileNotFoundError:
        pass
    return ""
