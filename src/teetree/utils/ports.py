import fcntl
import os
import socket
from pathlib import Path

_MAX_SCAN_DEPTH = 2
_DEFAULT_POSTGRES_PORT = 5432
_DEFAULT_REDIS_PORT = 6379

type ReservedPorts = dict[str, set[int]]


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", 0))
        return s.getsockname()[1]


def port_in_use(port: int) -> bool:
    for family in (socket.AF_INET, socket.AF_INET6):
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.bind(("localhost", port))
        except OSError:
            return True
        finally:
            sock.close()
    return False


def _parse_port_line(line: str, prefix: str) -> int | None:
    if not line.startswith(prefix):
        return None
    try:
        return int(line.split("=", 1)[1])
    except ValueError:
        return None


def _collect_used_ports(
    env_file: Path,
    used_backend: set[int],
    used_frontend: set[int],
    used_postgres: set[int],
) -> None:
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        value = _parse_port_line(line, "BACKEND_PORT=") or _parse_port_line(line, "DJANGO_RUNSERVER_PORT=")
        if value is not None:
            used_backend.add(value)
            continue
        value = _parse_port_line(line, "FRONTEND_PORT=")
        if value is not None:
            used_frontend.add(value)
            continue
        value = _parse_port_line(line, "POSTGRES_PORT=")
        if value is not None:
            used_postgres.add(value)
        continue


def _next_free_port(start: int, used_ports: set[int], *, check_system: bool) -> int:
    port = start
    while port in used_ports or (check_system and port_in_use(port)):
        used_ports.add(port)
        port += 1
    return port


def _find_free_ports_unlocked(
    workspace_dir: str,
    exclude_dir: str = "",
    *,
    check_system: bool,
    share_db_server: bool,
    reserved_ports: ReservedPorts | None = None,
) -> tuple[int, int, int, int]:
    workspace = Path(workspace_dir)
    used_backend: set[int] = set()
    used_frontend: set[int] = set()
    used_postgres: set[int] = set()

    for root, dirs, files in os.walk(workspace):
        depth = len(Path(root).relative_to(workspace).parts)
        if depth > _MAX_SCAN_DEPTH:
            dirs.clear()
            continue
        if ".env.worktree" not in files:
            continue
        env_file = Path(root) / ".env.worktree"
        if exclude_dir and str(env_file).startswith(f"{exclude_dir}/"):
            continue
        _collect_used_ports(env_file, used_backend, used_frontend, used_postgres)

    if reserved_ports:
        used_backend.update(reserved_ports.get("backend", set()))
        used_frontend.update(reserved_ports.get("frontend", set()))
        used_postgres.update(reserved_ports.get("postgres", set()))

    backend = _next_free_port(8001, used_backend, check_system=check_system)
    frontend = _next_free_port(4201, used_frontend, check_system=check_system)
    postgres = (
        _DEFAULT_POSTGRES_PORT if share_db_server else _next_free_port(5433, used_postgres, check_system=check_system)
    )
    return backend, frontend, postgres, _DEFAULT_REDIS_PORT


def find_free_ports(
    workspace_dir: str,
    exclude_dir: str = "",
    *,
    check_system: bool = True,
    share_db_server: bool = True,
    reserved_ports: ReservedPorts | None = None,
) -> tuple[int, int, int, int]:
    lock_file = Path(workspace_dir) / ".port-allocation.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    handle = lock_file.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return _find_free_ports_unlocked(
            workspace_dir,
            exclude_dir,
            check_system=check_system,
            share_db_server=share_db_server,
            reserved_ports=reserved_ports,
        )
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
