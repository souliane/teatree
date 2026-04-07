import platform
from pathlib import Path
from typing import cast

from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import get_overlay


def _docker_host_address() -> str:
    """Return the address Docker containers should use to reach the host.

    On macOS/Windows, ``host.docker.internal`` is resolved by Docker Desktop.
    On Linux, the standard Docker bridge gateway ``172.17.0.1`` is used.
    """
    if platform.system() in {"Darwin", "Windows"}:
        return "host.docker.internal"
    return "172.17.0.1"


def write_env_worktree(worktree: Worktree) -> str | None:
    """Write .env.worktree to the ticket directory and symlink it into the repo worktree.

    Contains non-port configuration only.  Port allocation happens at
    ``lifecycle start`` time and is managed by docker-compose.
    """
    extra = cast("dict[str, object]", worktree.extra or {})
    wt_path_str = extra.get("worktree_path")
    if not wt_path_str or not isinstance(wt_path_str, str):
        return None

    wt_path = Path(wt_path_str)
    ticket_dir = wt_path.parent
    envfile = ticket_dir / ".env.worktree"

    ticket = cast("Ticket", worktree.ticket)
    variant = ticket.variant or ""

    overlay = get_overlay()

    lines = [
        f"WT_VARIANT={variant}",
        f"TICKET_DIR={ticket_dir}",
        f"TICKET_URL={ticket.issue_url}",
        f"WT_DB_NAME={worktree.db_name}",
        f"COMPOSE_PROJECT_NAME={worktree.repo_path}-wt{ticket.ticket_number}",
    ]

    # When the overlay declares shared_postgres, containers must reach the
    # host's Postgres (not a per-worktree container).  Set POSTGRES_HOST to
    # the Docker-accessible host address so both host tooling (localhost) and
    # containers (host.docker.internal / 172.17.0.1) hit the same server.
    db_strategy = overlay.get_db_import_strategy(worktree)
    if db_strategy and db_strategy.get("shared_postgres"):
        docker_host = _docker_host_address()
        lines.append(f"POSTGRES_HOST={docker_host}")

    # Merge overlay env extras — overlay values override core defaults
    core_index = {line.split("=", 1)[0]: i for i, line in enumerate(lines)}
    for key, value in overlay.get_env_extra(worktree).items():
        if key in core_index:
            lines[core_index[key]] = f"{key}={value}"
        else:
            lines.append(f"{key}={value}")

    envfile.write_text("\n".join(lines) + "\n", encoding="utf-8")

    repo_envwt = wt_path / ".env.worktree"
    if repo_envwt.is_symlink() or repo_envwt.is_file():
        repo_envwt.unlink()
    repo_envwt.symlink_to(envfile)

    return str(envfile)
