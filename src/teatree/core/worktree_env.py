from pathlib import Path
from typing import cast

from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import get_overlay


def write_env_worktree(worktree: Worktree) -> str | None:
    """Write .env.worktree to the ticket directory and symlink it into the repo worktree."""
    extra = cast("dict[str, object]", worktree.extra or {})
    wt_path_str = extra.get("worktree_path")
    if not wt_path_str or not isinstance(wt_path_str, str):
        return None

    wt_path = Path(wt_path_str)
    ticket_dir = wt_path.parent
    envfile = ticket_dir / ".env.worktree"

    ticket = cast("Ticket", worktree.ticket)
    ports = worktree.ports or {}
    variant = ticket.variant or ""
    resolved_ports = {
        "be": ports.get("backend", 8000),
        "fe": ports.get("frontend", 4200),
        "pg": ports.get("postgres", 5432),
        "rd": ports.get("redis", 6379),
    }

    lines = [
        f"WT_VARIANT={variant}",
        f"TICKET_DIR={ticket_dir}",
        f"TICKET_URL={ticket.issue_url}",
        f"WT_DB_NAME={worktree.db_name}",
        f"BACKEND_PORT={resolved_ports['be']}",
        f"FRONTEND_PORT={resolved_ports['fe']}",
        f"POSTGRES_PORT={resolved_ports['pg']}",
        f"REDIS_PORT={resolved_ports['rd']}",
        f"DJANGO_RUNSERVER_PORT={resolved_ports['be']}",
        f"BACK_END_URL=http://localhost:{resolved_ports['be']}",
        f"FRONT_END_URL=http://localhost:{resolved_ports['fe']}",
        f"COMPOSE_PROJECT_NAME={worktree.repo_path}-wt{ticket.ticket_number}",
    ]
    # Append overlay env extras, but don't duplicate keys already set by core
    core_keys = {line.split("=", 1)[0] for line in lines}
    for key, value in get_overlay().get_env_extra(worktree).items():
        if key not in core_keys:
            lines.append(f"{key}={value}")

    envfile.write_text("\n".join(lines) + "\n", encoding="utf-8")

    repo_envwt = wt_path / ".env.worktree"
    if repo_envwt.is_symlink() or repo_envwt.is_file():
        repo_envwt.unlink()
    repo_envwt.symlink_to(envfile)

    return str(envfile)
