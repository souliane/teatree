"""Generate the per-ticket env cache.

The cache file lives at ``<ticket_dir>/.t3-cache/.t3-env.cache`` and is
regenerated on every ``t3 <overlay> worktree start``.  It is **not** the source of
truth — the DB is.  The file is ``chmod 444`` to discourage manual edits,
and its header calls out that edits are pointless.

Consumers (direnv, docker-compose, shell) need env as ``KEY=VALUE`` lines
at process start, so a file still has to exist on disk — but the shape of
this module makes the file actively inhospitable to being treated as
truth.
"""

import os
import platform
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from teatree.core.models import Ticket, Worktree, WorktreeEnvOverride
from teatree.core.models.types import WorktreeExtra, validated_worktree_extra
from teatree.core.overlay_loader import get_overlay_for_worktree
from teatree.utils.postgres_secret import PASS_KEY_ENV

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase

CACHE_DIRNAME = ".t3-cache"
CACHE_FILENAME = ".t3-env.cache"

_HEADER = (
    "# GENERATED — regenerated on every `t3 <overlay> worktree start`.\n"
    "# Edit the database via `t3 <overlay> env set` instead.  This file is chmod 444.\n"
    "# Source of truth: the Django DB (Ticket, Worktree, WorktreeEnvOverride).\n"
    "# Drift detection: `t3 <overlay> worktree start` refuses if file != DB render.\n"
    "#\n"
)


@dataclass(frozen=True, slots=True)
class EnvCacheSpec:
    """The declared env cache for a worktree.

    *path* is where the file was written. *keys* is the ordered tuple of
    keys rendered (one per line, no dupes). *content* is the full file
    body including header — useful for drift detection without a second
    disk read.
    """

    path: Path
    keys: tuple[str, ...]
    content: str


def compose_project(worktree: Worktree) -> str:
    """Return the docker-compose project name for *worktree*.

    Single source of truth for the project key — every consumer (the env cache,
    the stack gate, the reconciler, the start/cleanup runners, the CLI) resolves
    through here so the naming scheme can never drift across call sites.

    For a provisioned worktree the name is FROZEN on the immutable, unique
    ``Ticket.pk`` (``<repo_path>-wt<ticket.pk>``) and stored on
    ``Worktree.compose_project``, so two tickets sharing a trailing issue number
    never collide on one docker stack (the deferred half of #2774). The stored
    value is returned verbatim so a running stack's name never changes under it
    (a rename orphans its containers). Falls back to a live
    ``<repo_path>-wt<ticket.pk>`` derivation for a ticketless probe or an
    unprovisioned row that has no stored name yet.
    """
    stored = getattr(worktree, "compose_project", "")
    if stored:
        return stored
    ticket = getattr(worktree, "ticket", None)
    return f"{worktree.repo_path}-wt{ticket.pk}" if ticket else worktree.repo_path


def env_cache_path(worktree: Worktree) -> Path | None:
    """Return the canonical env-cache path ``<ticket_dir>/.t3-cache/.t3-env.cache``.

    ``None`` when the worktree has not been materialised on disk yet (no
    ``worktree_path``), so a caller checking the cache's presence can tell
    "not provisioned yet" apart from "provisioned but the cache is gone". The
    single home of the path computation — the aggregate provision post-condition
    and the diagnose/status commands resolve through here rather than
    re-joining the segments.
    """
    wt_path = worktree.worktree_path
    if not wt_path:
        return None
    return Path(wt_path).parent / CACHE_DIRNAME / CACHE_FILENAME


def _docker_host_address() -> str:
    """Return the address Docker containers should use to reach the host."""
    if platform.system() in {"Darwin", "Windows"}:
        return "host.docker.internal"
    return "172.17.0.1"


def _core_env_pairs(worktree: Worktree) -> list[tuple[str, str]]:
    """Return the key-value pairs that core contributes to every cache."""
    extra: WorktreeExtra = validated_worktree_extra(worktree.extra)
    wt_path_str = extra.get("worktree_path")
    if not wt_path_str:
        return []
    wt_path = Path(wt_path_str)
    ticket_dir = wt_path.parent
    ticket = cast("Ticket", worktree.ticket)

    return [
        ("WT_VARIANT", ticket.variant or ""),
        ("TICKET_DIR", str(ticket_dir)),
        ("TICKET_URL", ticket.issue_url),
        ("WT_DB_NAME", worktree.db_name),
        ("COMPOSE_PROJECT_NAME", compose_project(worktree)),
        (PASS_KEY_ENV, worktree.pass_key),
    ]


def _declared_core_keys() -> set[str]:
    """Return the fixed set of keys core always contributes."""
    return {
        "WT_VARIANT",
        "TICKET_DIR",
        "TICKET_URL",
        "WT_DB_NAME",
        "COMPOSE_PROJECT_NAME",
        "POSTGRES_HOST",
        PASS_KEY_ENV,
    }


def _check_overlay_does_not_collide_with_core(overlay: "OverlayBase") -> None:
    declared_overlay = overlay.declared_env_keys()
    duplicates = _declared_core_keys() & declared_overlay
    if duplicates:
        msg = (
            f"Overlay {overlay.__class__.__name__} declares keys that core "
            f"already owns: {sorted(duplicates)}. Remove them from the overlay."
        )
        raise RuntimeError(msg)


def render_env_cache(worktree: Worktree, *, overlay: "OverlayBase | None" = None) -> EnvCacheSpec | None:
    """Render the env cache content for *worktree* without touching disk.

    Returns ``None`` when the worktree has no ``worktree_path`` yet (not
    provisioned).  Used by drift detection and ``t3 teatree env show``.

    The overlay is resolved from the worktree's own ``overlay`` field
    (``get_overlay_for_worktree``) so this works on a multi-overlay host
    where a bare ``get_overlay()`` would raise ``Multiple overlays found``
    (souliane/teatree#1975). Callers that already hold the resolved
    instance — the provision/start runners — pass it via *overlay* to skip
    re-resolution.
    """
    extra: WorktreeExtra = validated_worktree_extra(worktree.extra)
    wt_path_str = extra.get("worktree_path")
    if not wt_path_str:
        return None
    ticket_dir = Path(wt_path_str).parent

    if overlay is None:
        overlay = get_overlay_for_worktree(worktree)
    pairs = dict(_core_env_pairs(worktree))

    db_strategy = overlay.get_db_import_strategy(worktree)
    if db_strategy and db_strategy.get("shared_postgres"):
        pairs["POSTGRES_HOST"] = _docker_host_address()

    _check_overlay_does_not_collide_with_core(overlay)
    pairs.update(overlay.get_env_extra(worktree))

    for cfg in overlay.get_base_images(worktree):
        pairs[cfg.env_var] = cfg.image_tag()

    pairs.update(load_overrides(worktree))

    # Drop secret keys from the on-disk cache — they remain in ``get_env_extra``
    # so subprocess callers (run backend, worktree_start) still receive them
    # via ``env=``, but the file at chmod 444 must not contain credentials.
    secret_keys = overlay.declared_secret_env_keys()
    ordered_keys = tuple(k for k in pairs if k not in secret_keys)
    body = "\n".join(f"{k}={pairs[k]}" for k in ordered_keys) + "\n"

    cache_path = ticket_dir / CACHE_DIRNAME / CACHE_FILENAME
    return EnvCacheSpec(path=cache_path, keys=ordered_keys, content=_HEADER + body)


def write_env_cache(worktree: Worktree, *, overlay: "OverlayBase | None" = None) -> EnvCacheSpec | None:
    """Write the env cache and copy it into the repo worktree.

    Idempotent.  Writes the file ``chmod 444``.  Callers that modify the
    DB should call this afterwards to refresh the cache.

    The in-worktree copy at ``<wt_path>/.t3-env.cache`` is a real file,
    not a symlink: when the worktree is bind-mounted into a Docker
    container, a symlink pointing at the host-absolute cache path
    dangles inside the container (errno 22 on stat). A real-file copy
    survives any mount layout. Drift detection still compares the
    canonical file under ``.t3-cache/`` against a fresh DB render, so
    the worktree-local copy is just a consumer-facing convenience.
    """
    spec = render_env_cache(worktree, overlay=overlay)
    if spec is None:
        return None

    extra: WorktreeExtra = validated_worktree_extra(worktree.extra)
    wt_path = Path(extra["worktree_path"])

    spec.path.parent.mkdir(parents=True, exist_ok=True)
    # Remove read-only bit before overwrite, then re-chmod 444.
    if spec.path.exists():
        spec.path.chmod(stat.S_IWUSR | stat.S_IRUSR)
    spec.path.write_text(spec.content, encoding="utf-8")
    spec.path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)  # 0o444

    repo_copy = wt_path / CACHE_FILENAME
    if repo_copy.is_symlink() or repo_copy.exists():
        repo_copy.unlink()
    shutil.copy2(spec.path, repo_copy)

    return spec


def worktree_pg_connection(
    worktree: Worktree, *, overlay: "OverlayBase | None" = None
) -> tuple[str, str, dict[str, str]]:
    """Resolve ``(user, host, env)`` for connecting to *worktree*'s postgres.

    The worktree's overlay decides the connecting role, host and port
    via ``get_env_extra`` (an overlay may connect as a non-default
    superuser role on ``localhost``). The bare process-env defaults in
    ``utils.db`` fall back to ``postgres`` / ``localhost`` — a role that
    need not exist on the host — so a per-worktree existence check must
    connect with the overlay's resolved params, not the defaults.

    Returns ``("", "", {})`` for an unprovisioned worktree so callers fall
    back to the plain ``db_exists`` defaults.
    """
    from teatree.utils.db import pg_env  # noqa: PLC0415

    extra: WorktreeExtra = validated_worktree_extra(worktree.extra)
    if not extra.get("worktree_path"):
        return "", "", {}

    if overlay is None:
        overlay = get_overlay_for_worktree(worktree)
    resolved = dict(overlay.get_env_extra(worktree))

    env = {**os.environ, **resolved}
    env.pop("VIRTUAL_ENV", None)
    return resolved.get("POSTGRES_USER", ""), resolved.get("POSTGRES_HOST", ""), pg_env(env)


def detect_drift(worktree: Worktree, *, overlay: "OverlayBase | None" = None) -> tuple[bool, Path | None]:
    """Return ``(is_drifted, cache_path)``.

    Drift = file on disk differs from a fresh DB render, OR file is
    missing.  Returns ``(False, None)`` for unprovisioned worktrees.
    """
    spec = render_env_cache(worktree, overlay=overlay)
    if spec is None:
        return False, None
    if not spec.path.is_file():
        return True, spec.path
    on_disk = spec.path.read_text(encoding="utf-8")
    return on_disk != spec.content, spec.path


def set_override(worktree: Worktree, key: str, value: str) -> None:
    """Persist a ``WorktreeEnvOverride`` row and refresh the cache."""
    reserved = _declared_core_keys()
    if key in reserved:
        msg = f"{key} is owned by core — edit the model field, not the env cache."
        raise ValueError(msg)

    WorktreeEnvOverride.objects.update_or_create(
        worktree=worktree,
        key=key,
        defaults={"value": value},
    )
    write_env_cache(worktree)


def load_overrides(worktree: Worktree) -> dict[str, str]:
    """Return user-provided overrides for *worktree*."""
    return dict(WorktreeEnvOverride.objects.filter(worktree=worktree).values_list("key", "value"))
