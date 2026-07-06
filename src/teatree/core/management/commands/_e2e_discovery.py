"""Frontend port discovery and linked-ticket env routing for ``e2e external``.

Split out of ``e2e.py`` to keep that module under the project's per-file
LOC cap and to give the linked-ticket plumbing introduced in #1322 a clear
home: the helpers here decide *where* the backend stack lives (which
compose project, which env cache, which on-disk worktree path), and the
``e2e`` command module wires them into the CLI.
"""

import socket

from teatree.core.models import Ticket, Worktree
from teatree.core.resolve import _find_env_cache, _parse_env_file
from teatree.core.worktree.worktree_env import compose_project
from teatree.utils.ports import get_service_port


def detect_local_port(port: int) -> int | None:
    """Return *port* if something is listening on localhost, else None."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return port
    return None


def compose_frontend_port(project: str) -> int | None:
    """Return the published host port for *project*'s ``frontend`` service.

    The compose ``frontend`` service is nginx serving the pre-built dist on
    container port 80; a raw dev-server setup instead listens on 4200.
    """
    for container_port in (80, 4200):
        port = get_service_port(project, "frontend", container_port)
        if port is not None:
            return port
    return None


def ticket_frontend_projects(worktree: Worktree, *, linked_ticket: Ticket | None = None) -> list[str]:
    """Compose projects that may host the frontend for this worktree's ticket.

    The resolved worktree is whatever the cwd matched — for an external test
    repo that is the *test* worktree, whose compose project has no frontend.
    The frontend lives in a sibling repo's worktree under the same ticket, so
    probe the resolved worktree first, then every sibling under the ticket.

    ``linked_ticket`` (#1322): when the resolved worktree is an out-of-tree
    e2e cache repo that is not DB-linked to the backend ticket (a frequent
    shape — the user calls ``e2e external`` from the cache dir, whose
    auto-registered worktree belongs to ``auto:<branch>`` or a different
    ticket), the caller can name the backend ticket explicitly. Discovery
    then routes at *that* ticket's worktrees and bypasses the resolved
    ticket's siblings.
    """
    if linked_ticket is not None:
        candidates: list[Worktree] = [worktree, *Worktree.objects.filter(ticket=linked_ticket).order_by("pk")]
    else:
        ticket = worktree.ticket
        candidates = [worktree]
        if ticket is not None:
            candidates += [wt for wt in Worktree.objects.filter(ticket=ticket) if wt.pk != worktree.pk]
    seen: set[str] = set()
    projects: list[str] = []
    for wt in candidates:
        project = compose_project(wt)
        if project not in seen:
            seen.add(project)
            projects.append(project)
    return projects


def discover_frontend_port(worktree: Worktree, *, linked_ticket: Ticket | None = None) -> int | None:
    """Discover the frontend port for a worktree's stack.

    ``docker compose port`` is authoritative when the stack is up; the local
    scan is a last-ditch fallback for users who started compose outside the
    teatree runner. The frontend may be served by a sibling repo's compose
    project under the same ticket, so every ticket project is probed.

    ``linked_ticket`` (#1322): see ``ticket_frontend_projects`` — re-routes
    discovery at the named ticket's worktrees when the resolved worktree is
    not DB-linked to the backend stack.
    """
    for project in ticket_frontend_projects(worktree, linked_ticket=linked_ticket):
        port = compose_frontend_port(project)
        if port is not None:
            return port
    # Scan the allocation range — ports start at 4200 and go up
    for candidate in range(4200, 4211):
        if detect_local_port(candidate) is not None:
            return candidate
    return None


def _runs_backend_stack(worktree: Worktree) -> bool:
    """True when this worktree's compose project runs the backend (``web``) service.

    Overlay-agnostic signal: the overlay returns a non-empty compose file only
    for the worktree that owns the backend stack (a frontend / data-only repo
    returns ``""``). ``docker compose exec web`` and the exported
    ``COMPOSE_PROJECT_NAME`` must target *that* worktree.
    """
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    try:
        return bool(get_overlay().get_compose_file(worktree))
    except Exception:  # noqa: BLE001 — a misbehaving overlay hook must not break routing
        return False


def resolve_linked_worktree(linked_ticket: Ticket) -> Worktree | None:
    """Pick the worktree that owns the backend stack for ``linked_ticket``.

    The env cache that feeds ``get_e2e_env_extras`` and the
    ``COMPOSE_PROJECT_NAME`` exported for ``docker compose`` calls both live
    on this worktree. A multi-repo ticket has several siblings, and the first
    by pk is often the *frontend* worktree — exporting its compose project as
    ``COMPOSE_PROJECT_NAME`` makes ``docker compose exec web`` fail with
    "service web is not running" (#1322). Prefer the sibling that actually
    runs the backend stack (non-empty overlay compose file); fall back to the
    first stored-path worktree, then any sibling so a freshly-provisioned
    ticket with no recorded ``worktree_path`` still routes.
    """
    siblings = list(Worktree.objects.filter(ticket=linked_ticket).order_by("pk"))
    stored = [wt for wt in siblings if (wt.extra or {}).get("worktree_path")]
    for wt in stored:
        if _runs_backend_stack(wt):
            return wt
    if stored:
        return stored[0]
    return siblings[0] if siblings else None


def linked_env_cache(linked_worktree: Worktree | None) -> dict[str, str]:
    """Read the env cache that lives on ``linked_worktree``'s on-disk path.

    Returns an empty dict when the worktree has no recorded path or the
    cache file is absent; the caller is responsible for deciding whether
    that is a fatal misconfig.
    """
    if linked_worktree is None:
        return {}
    wt_path = (linked_worktree.extra or {}).get("worktree_path", "")
    if not wt_path:
        return {}
    envfile = _find_env_cache(str(wt_path))
    return _parse_env_file(envfile) if envfile is not None else {}
