"""What a run's inputs resolve to before any runner is chosen.

Five decisions the ``e2e`` command makes before it dispatches: which dual-env
target was asked for, which backend ticket a run is linked to, where its
artifacts belong, which frontend port serves it, and the per-target trio the
environment is built from. They share one shape — read the user's input, refuse
with an exit code the caller cannot mistake for a value, or return the resolved
answer — so they live together and the command keeps only the dispatch.

Each takes ``write`` rather than a command, so nothing here needs Django's
management plumbing to be exercised.
"""

import os
from collections.abc import Callable
from pathlib import Path

from teatree.core.intake.resolve import resolve_worktree
from teatree.core.management.commands import _e2e_discovery as _disc
from teatree.core.management.commands import _e2e_runners as _runners
from teatree.core.models import Ticket, Worktree
from teatree.core.worktree.worktree_env import compose_project
from teatree.utils.ports import host_published_port_host

#: Bound at module level under the names the command re-exports, so a caller can
#: patch one attribute here and reach the call this module actually makes.
_discover_frontend_port = _disc.discover_frontend_port
_ticket_frontend_projects = _disc.ticket_frontend_projects
_resolve_linked_worktree = _disc.resolve_linked_worktree
_linked_env_cache = _disc.linked_env_cache

Write = Callable[[str], None]
RequirePort = Callable[[Worktree, "Ticket | None"], int]

_REMOTE_TARGETS = frozenset({"dev", "qa"})
_TARGETS = frozenset({"dev", "qa", "local"})


def require_frontend_port(worktree: Worktree, linked_ticket: "Ticket | None", *, write: Write) -> int:
    port = _discover_frontend_port(worktree, linked_ticket=linked_ticket)
    if port is None:
        probed = ", ".join(_ticket_frontend_projects(worktree, linked_ticket=linked_ticket)) or "none"
        write(
            f"Frontend not running (no docker `frontend` service in [{probed}], "
            "no local process on 4200). Run `t3 <overlay> worktree start` first.",
        )
        raise SystemExit(1)
    return port


def resolve_target_env(
    resolved_target: str,
    linked_ticket: "Ticket | None",
    *,
    write: Write,
    require_port: RequirePort,
) -> tuple[str | None, str | None, dict[str, str] | None]:
    """Build the per-target trio passed to ``build_e2e_env``."""
    if resolved_target in _REMOTE_TARGETS:
        if not os.environ.get("BASE_URL"):
            write(f"--target {resolved_target} requires BASE_URL (the deployed environment URL) to be set.")
            raise SystemExit(1)
        return None, None, None

    # The frontend port is published on the DOCKER HOST. `localhost` names that
    # host only when the CLI runs natively; from inside the containerized CLI it is
    # the container's own loopback, where nothing listens.
    host = host_published_port_host()

    if linked_ticket is not None:
        linked_wt = _resolve_linked_worktree(linked_ticket)
        if linked_wt is not None:
            port = require_port(linked_wt, linked_ticket)
            return f"http://{host}:{port}", compose_project(linked_wt), _linked_env_cache(linked_wt)

    worktree = resolve_worktree()
    port = require_port(worktree, linked_ticket)
    return f"http://{host}:{port}", compose_project(worktree), None


def resolve_linked_ticket(linked_to: int, *, write: Write) -> "Ticket | None":
    """Resolve ``--linked-to <pk>`` to a Ticket or exit on misconfig.

    ``0`` means "no link" — return None (back-compat path). A non-zero pk that
    misses must fail fast: silently falling through would mask the user's intent
    to route at a specific backend stack.
    """
    if not linked_to:
        return None
    try:
        return Ticket.objects.get(pk=linked_to)
    except Ticket.DoesNotExist:
        write(
            f"--linked-to ticket pk={linked_to} not found. "
            "Pass the backend ticket's pk (see `t3 <overlay> ticket list`).",
        )
        raise SystemExit(2) from None


def resolve_artifacts_dir(explicit: str, *, write: Write) -> str:
    """Resolve the out-of-repo E2E artifacts root the runner exports (#3331).

    An explicit ``--artifacts-dir`` is honoured but REFUSED when it resolves
    inside a repo working tree (captures never live in a source tree). Empty
    derives ``<ticket_dir>/.t3-cache/artifacts`` from the resolved worktree; an
    unresolvable worktree yields ``""`` (the var is simply not exported).
    """
    if explicit:
        try:
            _runners.refuse_artifacts_dir_in_repo(Path(explicit))
        except _runners.ArtifactsDirInRepoError as exc:
            write(str(exc))
            raise SystemExit(2) from exc
        return str(Path(explicit).expanduser())
    try:
        worktree = resolve_worktree()
    except Exception:  # noqa: BLE001 — an unresolvable worktree degrades to no artifacts dir, never aborts
        return ""
    wt_path = (worktree.extra or {}).get("worktree_path", "") if worktree else ""
    return str(_runners.e2e_artifacts_root(wt_path)) if wt_path else ""


def resolve_target(target: str, *, write: Write) -> str:
    """Resolve the dual-env target deterministically.

    Explicit values are ``dev`` / ``qa`` / ``local``. Empty preserves the
    back-compat inference: ``BASE_URL`` means remote ``dev``, else ``local``.
    """
    normalized = target.strip().lower()
    if normalized in _TARGETS:
        return normalized
    if normalized:
        write(f"--target must be 'dev', 'qa', or 'local', got {target!r}.")
        raise SystemExit(2)
    return "dev" if os.environ.get("BASE_URL") else "local"
