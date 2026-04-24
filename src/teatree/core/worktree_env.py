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

import platform
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from teatree.core.models import Ticket, Worktree, WorktreeEnvOverride
from teatree.core.models.types import WorktreeExtra, validated_worktree_extra
from teatree.core.overlay_loader import get_overlay
from teatree.docker.build import image_tag_for_lockfile

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

    pairs: list[tuple[str, str]] = [
        ("WT_VARIANT", ticket.variant or ""),
        ("TICKET_DIR", str(ticket_dir)),
        ("TICKET_URL", ticket.issue_url),
        ("WT_DB_NAME", worktree.db_name),
        ("COMPOSE_PROJECT_NAME", f"{worktree.repo_path}-wt{ticket.ticket_number}"),
    ]
    if ticket.redis_db_index is not None:
        pairs.append(("REDIS_DB_INDEX", str(ticket.redis_db_index)))
    return pairs


def _declared_core_keys() -> set[str]:
    """Return the fixed set of keys core always contributes."""
    return {
        "WT_VARIANT",
        "TICKET_DIR",
        "TICKET_URL",
        "WT_DB_NAME",
        "COMPOSE_PROJECT_NAME",
        "REDIS_DB_INDEX",
        "POSTGRES_HOST",
    }


def render_env_cache(worktree: Worktree) -> EnvCacheSpec | None:
    """Render the env cache content for *worktree* without touching disk.

    Returns ``None`` when the worktree has no ``worktree_path`` yet (not
    provisioned).  Used by drift detection and ``t3 env show``.
    """
    extra: WorktreeExtra = validated_worktree_extra(worktree.extra)
    wt_path_str = extra.get("worktree_path")
    if not wt_path_str:
        return None
    wt_path = Path(wt_path_str)
    ticket_dir = wt_path.parent

    overlay = get_overlay()
    pairs = dict(_core_env_pairs(worktree))

    db_strategy = overlay.get_db_import_strategy(worktree)
    if db_strategy and db_strategy.get("shared_postgres"):
        pairs["POSTGRES_HOST"] = _docker_host_address()

    declared_core = _declared_core_keys()
    declared_overlay = overlay.declared_env_keys()
    duplicates = declared_core & declared_overlay
    if duplicates:
        msg = (
            f"Overlay {overlay.__class__.__name__} declares keys that core "
            f"already owns: {sorted(duplicates)}. Remove them from the overlay."
        )
        raise RuntimeError(msg)

    pairs.update(overlay.get_env_extra(worktree))

    for cfg in overlay.get_base_images(worktree):
        pairs[cfg.env_var] = image_tag_for_lockfile(cfg)

    ordered_keys = tuple(pairs.keys())
    body = "\n".join(f"{k}={pairs[k]}" for k in ordered_keys) + "\n"
    content = _HEADER + body

    cache_path = ticket_dir / CACHE_DIRNAME / CACHE_FILENAME
    return EnvCacheSpec(path=cache_path, keys=ordered_keys, content=content)


def write_env_cache(worktree: Worktree) -> EnvCacheSpec | None:
    """Write the env cache and symlink it into the repo worktree.

    Idempotent.  Writes the file ``chmod 444``.  Callers that modify the
    DB should call this afterwards to refresh the cache.
    """
    spec = render_env_cache(worktree)
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

    repo_link = wt_path / CACHE_FILENAME
    if repo_link.is_symlink() or repo_link.exists():
        repo_link.unlink()
    repo_link.symlink_to(spec.path)

    return spec


def detect_drift(worktree: Worktree) -> tuple[bool, Path | None]:
    """Return ``(is_drifted, cache_path)``.

    Drift = file on disk differs from a fresh DB render, OR file is
    missing.  Returns ``(False, None)`` for unprovisioned worktrees.
    """
    spec = render_env_cache(worktree)
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
