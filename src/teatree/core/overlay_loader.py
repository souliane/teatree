"""Discover and cache overlay instances from entry points and TOML config.

Unifies both discovery mechanisms so that ``get_overlay()`` works regardless
of whether the overlay was registered via ``pip install`` (entry point) or
``~/.teatree.toml`` (TOML config).
"""

import importlib
import logging
import os
import tempfile
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from django.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from collections.abc import Iterator

    from teatree.core.models import Ticket, Worktree
    from teatree.core.overlay import OverlayBase

logger = logging.getLogger(__name__)


def get_overlay(name: str | None = None) -> "OverlayBase":
    overlays = _discover_overlays()
    if not overlays:
        msg = (
            "No teatree overlays found. Install a package that provides a"
            " 'teatree.overlays' entry point, or add one to ~/.teatree.toml."
        )
        raise ImproperlyConfigured(msg)

    if name is None:
        name = os.environ.get("T3_OVERLAY_NAME") or None

    if name is not None:
        try:
            return overlays[name]
        except KeyError:
            msg = f"Overlay {name!r} not found. Available: {', '.join(sorted(overlays))}"
            raise ImproperlyConfigured(msg) from None

    if len(overlays) == 1:
        return next(iter(overlays.values()))

    msg = f"Multiple overlays found ({', '.join(sorted(overlays))}). Pass an explicit name to get_overlay()."
    raise ImproperlyConfigured(msg)


def _canonical_overlay_name(name: str) -> str | None:
    """Fold a stored overlay name onto its canonical registered name, or ``None``.

    A blank name is the ambient single-overlay default (``None`` → the
    caller lets ``get_overlay(None)`` resolve it). A non-blank name is
    canonicalized through the same :func:`resolve_overlay_name` rule the
    config loader uses, so a stored legacy alias (``teatree`` → ``t3-teatree``)
    resolves instead of raising ``Overlay not found``. An unresolvable name
    is returned unchanged so ``get_overlay`` raises a precise ``not found``.
    """
    if not name:
        return None
    return resolve_overlay_name(name) or name


def get_overlay_for_ticket(ticket: "Ticket") -> "OverlayBase":
    """Resolve the overlay a ticket belongs to.

    The queued FSM workers run a ticket's runners in a process where every
    installed overlay is registered, so a bare :func:`get_overlay` raises
    ``Multiple overlays found`` (souliane/teatree#1814). The ticket records
    its own overlay, so resolution is unambiguous regardless of how many
    overlays are installed; an empty value falls through to the ambient
    single-overlay default. A stored legacy alias is folded onto its
    canonical registered name (souliane/teatree#1975).
    """
    return get_overlay(_canonical_overlay_name(ticket.overlay))


def get_overlay_for_worktree(worktree: "Worktree") -> "OverlayBase":
    """Resolve the overlay a worktree belongs to.

    Like :func:`get_overlay_for_ticket` but keyed on the worktree's own
    ``overlay`` field, falling back to the owning ticket for rows created
    before the field was populated (souliane/teatree#1814).
    """
    if worktree.overlay:
        return get_overlay(_canonical_overlay_name(worktree.overlay))
    return get_overlay_for_ticket(worktree.ticket)


def get_overlay_for_repo(repo: str = ".") -> "OverlayBase | None":
    """Return the overlay whose workspace repos own the git repo at ``repo``.

    Resolves the ``origin`` remote slug (``owner/name``) of the repo at
    ``repo`` and matches it against each registered overlay's
    ``get_workspace_repos()`` — the same repo-ownership relation
    :func:`infer_overlay_for_url` uses for a URL. This lets a caller in an
    ambiguous multi-overlay environment pick the overlay that actually owns
    the current repository instead of crashing on ambiguity.

    Returns ``None`` when the slug is empty (no ``origin``) or matches zero
    or more than one overlay, so the caller can fall back deterministically
    rather than guess wrong. An overlay whose ``get_workspace_repos()``
    raises is skipped so one broken overlay can't poison resolution.
    """
    from teatree.utils.git import remote_slug  # noqa: PLC0415

    slug = remote_slug(repo=repo)
    if not slug:
        return None

    matches: list[OverlayBase] = []
    for name, overlay in get_all_overlays().items():
        getter = getattr(overlay, "get_workspace_repos", None)
        if not callable(getter):
            continue
        try:
            repo_slugs = getter()
        except Exception:
            logger.warning("Overlay %r get_workspace_repos() failed during repo resolution", name, exc_info=True)
            continue
        for repo_slug in repo_slugs or []:
            if isinstance(repo_slug, str) and repo_slug and repo_slug in slug:
                matches.append(overlay)
                break

    if len(matches) == 1:
        return matches[0]
    return None


def get_all_overlays() -> "dict[str, OverlayBase]":
    return dict(_discover_overlays())


def get_all_overlay_names() -> list[str]:
    """Return all overlay names, including path-only TOML entries.

    Unlike ``get_all_overlays()``, this includes TOML entries that declare a
    ``path`` but no ``class`` — they can't be instantiated as OverlayBase but
    should appear when listing overlays known to teatree (for ticket filtering, etc.).
    """
    from teatree.config import load_config  # noqa: PLC0415

    names = set(_discover_overlays())
    config = load_config()
    for name, cfg in config.raw.get("overlays", {}).items():
        if cfg.get("path"):
            names.add(name)
    return sorted(names)


def resolve_overlay_name(name: str) -> str | None:
    """Return the canonical registered overlay name for *name*, or ``None``.

    The single source of truth for "is this overlay name dispatchable, and
    under what canonical name". A name that is already a registered overlay
    returns unchanged; a legacy short alias folds onto its registered
    entry-point via the same ``_match_canonical_ep`` rule the config loader
    uses (``teatree`` → ``t3-teatree``). A name that matches nothing — a
    removed overlay, a synthetic scanner tag, a typo — returns ``None`` so
    callers can fail it permanently instead of crashing on every retry
    (souliane/teatree#1959 poison-pill).

    Callers asking only "is this dispatchable?" test ``resolve_overlay_name(x)
    is not None``; an empty/blank ``name`` is the ambient single-overlay default
    and is the caller's responsibility to special-case (it returns ``None``).
    """
    from teatree.config import _match_canonical_ep  # noqa: PLC0415

    if not name:
        return None
    known = set(get_all_overlay_names())
    if name in known:
        return name
    return _match_canonical_ep(name, known)


def infer_overlay_for_url(url: str) -> str:
    """Return the overlay whose workspace repos own ``url``, or ``""``.

    Routes through ``overlay.get_workspace_repos()`` rather than the raw
    ``config.workspace_repos`` attribute: overlays that compute their repo
    list dynamically leave the attribute empty, so reading it directly
    mis-attributes every one of their tickets. A registered entry that is
    not a full overlay, or whose hook raises, is skipped so one broken
    overlay can't poison inference for the others.
    """
    if not url:
        return ""
    for name, overlay in get_all_overlays().items():
        getter = getattr(overlay, "get_workspace_repos", None)
        if not callable(getter):
            continue
        try:
            repo_slugs = getter()
        except Exception:
            logger.warning("Overlay %r get_workspace_repos() failed during inference", name, exc_info=True)
            continue
        for repo_slug in repo_slugs or []:
            if isinstance(repo_slug, str) and repo_slug in url:
                return name
    return ""


@lru_cache(maxsize=1)
def _discover_overlays() -> "dict[str, OverlayBase]":
    import importlib.metadata  # noqa: PLC0415

    from teatree.core.overlay import OverlayBase  # noqa: PLC0415

    result: dict[str, OverlayBase] = {}

    # 1. Entry-point overlays (pip-installed packages)
    eps = importlib.metadata.entry_points(group="teatree.overlays")
    for ep in eps:
        cls = ep.load()
        if not issubclass(cls, OverlayBase):
            msg = f"Entry point {ep.name!r} ({ep.value}) does not subclass OverlayBase"
            raise ImproperlyConfigured(msg)
        overlay = cls()
        # Apply [overlays.<name>] TOML overrides so entry-point overlays
        # are configurable from ~/.teatree.toml on the same footing as
        # TOML-only overlays. Without this, OverlayConfig subclasses
        # would have to opt in by passing overlay_name to super().__init__.
        overlay.config.apply_toml_overrides(ep.name)
        result[ep.name] = overlay

    # 2. TOML-configured overlays (not already found via entry points)
    result.update(_discover_toml_overlays(OverlayBase, set(result)))

    return result


def _discover_toml_overlays(
    base_class: type["OverlayBase"],
    already_found: set[str],
) -> "dict[str, OverlayBase]":
    """Discover overlays from ``~/.teatree.toml`` that aren't already entry-point-registered."""
    from teatree.config import load_config  # noqa: PLC0415

    result: dict[str, OverlayBase] = {}
    config = load_config()
    overlays_cfg = config.raw.get("overlays", {})

    for name, overlay_cfg in overlays_cfg.items():
        if name in already_found:
            continue

        class_path = overlay_cfg.get("class", "")
        if not class_path or ":" not in class_path:
            # No class path — this is a project-directory-only overlay without
            # a Python class.  These work through the CLI subprocess bridge
            # (OverlayAppBuilder) but can't be instantiated as OverlayBase.
            continue

        try:
            module_path, class_name = class_path.rsplit(":", 1)
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            if not issubclass(cls, base_class):
                logger.warning("TOML overlay %r class %s does not subclass OverlayBase", name, class_path)
                continue
            result[name] = cls()
        except (ImportError, AttributeError) as exc:
            logger.warning("TOML overlay %r failed to load class %s: %s", name, class_path, exc)

    return result


def reset_overlay_cache() -> None:
    """Fully reset overlay discovery state so the next call rebuilds from scratch.

    Two layers must reset together. First, ``_discover_overlays.cache_clear()``
    drops the cached ``dict`` of overlay instances so the next ``get_overlay()``
    rediscovers entry points and re-instantiates overlays. Second, the bundled
    overlay module ``teatree.contrib.t3_teatree.overlay`` is dropped from
    ``sys.modules`` so its class body re-evaluates on the next import.
    ``TeatreeOverlay.config`` is a *class-level* :class:`OverlayConfig`
    singleton built at class-definition time against the live
    ``teatree.config.load_config()``. If the module stays cached in
    ``sys.modules`` the class attribute survives test teardown — and any test
    that ``patch("teatree.config.load_config")`` then instantiates
    ``TeatreeOverlay()`` silently sees the stale pre-patch config. Popping the
    module forces the class body to re-evaluate under the patched ``load_config``.

    Production code never calls this function — it's a test-isolation
    helper. The ``sys.modules`` pop is therefore safe: at runtime the
    module imports once and stays; under pytest it gets a clean rebuild
    between tests, which is exactly what test isolation requires.

    Keeping the full reset behind this single entry point means
    conftests don't need to know about class-cache vs lru_cache vs
    module-level evaluation — they just call ``reset_overlay_cache()``
    and get a clean slate. See souliane/teatree#1108 for the test-
    pollution incident this design closes (originally surfaced by
    ``slack_bridge_e2e``, then independently by ``tests/teatree_core``).
    """
    import sys  # noqa: PLC0415

    _discover_overlays.cache_clear()
    sys.modules.pop("teatree.contrib.t3_teatree.overlay", None)


@contextmanager
def staged_overlay_autonomy(overlay_name: str, autonomy: str) -> "Iterator[None]":
    """Run the block with a hermetic ``~/.teatree.toml`` pinning *overlay_name* to *autonomy*.

    A test-isolation helper for assertions whose outcome depends on the
    overlay's effective autonomy (the substrate-merge carve-out). Swaps
    :data:`teatree.config.CONFIG_PATH` at the single seam ``get_effective_settings``
    reads, so the resolved autonomy is deterministic regardless of the
    developer's live config, and restores it on exit. Production code never
    calls this.
    """
    from teatree import config as config_module  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as raw:
        cfg = Path(raw) / ".teatree.toml"
        cfg.write_text(f'[teatree]\n[overlays.{overlay_name}]\nautonomy = "{autonomy}"\n', encoding="utf-8")
        original = config_module.CONFIG_PATH
        config_module.CONFIG_PATH = cfg
        try:
            yield
        finally:
            config_module.CONFIG_PATH = original
