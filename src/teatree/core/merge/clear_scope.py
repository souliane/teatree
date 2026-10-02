"""Which overlay a ``MergeClear`` belongs to — scoped by what the row actually carries.

``MergeClear.ticket`` is nullable and, on the dominant self-merge convention, null is
the NORM: measured on the live control DB for #4250, 599 of 622 CLEARs and 87 of 87
UNCONSUMED ones carried ``ticket=None``. Every overlay-scoped reader that joined
``ticket__overlay`` therefore matched nothing and reported healthy over a 19-day-old
merge backlog.

The row does carry its own ``slug``, so scoping rides that instead. Resolution mirrors
:func:`~teatree.core.merge.pr_slug_resolution.resolve_pr_repo_slug` — an ``owner/repo``
slug is itself, a workstream slug resolves to the running clone's origin — with the
clone origin resolved ONCE per scope rather than per row, so widening the population
costs no extra git subprocess.
"""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from teatree.core.merge.pr_slug_resolution import _looks_like_owner_repo, _project_repo_slug, normalize_repo_slug
from teatree.core.overlay_loader import get_all_overlays

if TYPE_CHECKING:
    from teatree.core.models.merge_clear import MergeClear
    from teatree.core.overlay import OverlayBase

logger = logging.getLogger(__name__)

#: The declaration surfaces an overlay names its repos on, read as ``facet.method``.
#: ``get_workspace_repos`` is on the base; the other two are faceted. Every one is
#: best-effort — an overlay that declares none yields an empty set, which the
#: predicate reads as "cannot attribute", never as "owns nothing".
_REPO_DECLARATIONS: tuple[tuple[str, str], ...] = (
    ("", "get_workspace_repos"),
    ("review", "merge_candidate_repo_slugs"),
    ("metadata", "get_followup_repos"),
)


def _declared(overlay: "OverlayBase", facet: str, method: str) -> list[str]:
    target = getattr(overlay, facet) if facet else overlay
    getter = getattr(target, method, None)
    if not callable(getter):
        return []
    try:
        declared: object = getter()
    except Exception:
        logger.warning("overlay %s.%s failed while scoping CLEARs", facet or "base", method, exc_info=True)
        return []
    if not isinstance(declared, list | tuple):
        return []
    return [value for value in declared if isinstance(value, str)]


def overlay_repo_slugs(overlay: str) -> frozenset[str]:
    """Every ``owner/repo`` *overlay* declares, canonicalized UP; empty when unknown.

    Unions the three surfaces an overlay names its repos on, each normalized to the
    fully-qualified ``owner/repo`` via :func:`normalize_repo_slug` — a bare directory
    token (``teatree``) yields nothing rather than being matched by stripping the
    other side down. An unregistered overlay, or one that declares no repo anywhere,
    returns an empty set.
    """
    if not overlay:
        return frozenset()
    try:
        registered = get_all_overlays().get(overlay)
    except Exception:
        logger.warning("overlay discovery failed while scoping CLEARs to %r", overlay, exc_info=True)
        return frozenset()
    if registered is None:
        return frozenset()
    slugs = {
        normalize_repo_slug(value)
        for facet, method in _REPO_DECLARATIONS
        for value in _declared(registered, facet, method)
    }
    return frozenset(slug for slug in slugs if slug)


class _ClearScope:
    """Callable "does this CLEAR belong to *overlay*?" with the clone origin memoized."""

    def __init__(self, overlay: str) -> None:
        self._overlay = overlay
        self._owned = overlay_repo_slugs(overlay)
        self._origin: str | None = None

    def _clone_origin(self) -> str:
        if self._origin is None:
            try:
                self._origin = _project_repo_slug()
            except Exception:
                logger.warning("clone-origin probe failed while scoping CLEARs", exc_info=True)
                self._origin = ""
        return self._origin

    def __call__(self, clear: "MergeClear") -> bool:
        ticket = clear.ticket
        if ticket is not None:
            return ticket.overlay == self._overlay
        if not self._owned:
            # No declared repo means the CLEAR cannot be attributed either way, and an
            # unattributable stalled merge reported under the wrong overlay still gets
            # read — one reported under none does not. Silence is the defect.
            return True
        slug = str(clear.slug or "")
        repo = slug if _looks_like_owner_repo(slug) else self._clone_origin()
        return repo in self._owned


def _every_clear(clear: "MergeClear") -> bool:
    _ = clear
    return True


def clear_scope_predicate(overlay: str) -> Callable[["MergeClear"], bool]:
    """The single "is this CLEAR *overlay*'s?" predicate every scoped reader shares.

    A blank *overlay* is the global view and matches everything. Otherwise a CLEAR
    carrying a ticket keeps the exact ``ticket.overlay == overlay`` semantics it always
    had, and a ticket-less one is attributed by its repo.
    """
    if not overlay:
        return _every_clear
    return _ClearScope(overlay)
