"""DSLR snapshot helpers for ``t3 workspace clean-all`` (#1306).

Split from :mod:`_workspace_cleanup` so the public-functions-per-module
cap stays clear. Functions here own the "is this tenant in use?" guard
that prevents :func:`prune_dslr_snapshots` from destroying a snapshot
an in-flight worktree is about to restore from.
"""

from teatree.core.models import Worktree
from teatree.core.overlay_loader import get_overlay


def dslr_tenants_in_use() -> set[str]:
    """Return DSLR tenants in use by an in-flight worktree.

    A worktree in CREATED state (mid-provision, DB not yet imported) is
    about to restore from a DSLR snapshot whose tenant suffix is derived
    from the ticket's variant. Pruning that snapshot before the worktree
    completes provisioning leaves no recovery path. This helper returns
    the set of tenant strings to skip in
    :func:`prune_dslr_snapshots`.

    The mapping ``variant → tenant`` is overlay-specific: an overlay may
    prefix the variant (``client-a`` → ``development-client-a``) while
    others return the variant verbatim. We consult
    :meth:`OverlayBase.get_dslr_tenant_for_variant` per active variant.
    """
    try:
        overlay = get_overlay()
    except Exception:  # noqa: BLE001 — best effort; pruning continues if no overlay
        return set()
    variants = {
        wt.ticket.variant or ""
        for wt in Worktree.objects.filter(state=Worktree.State.CREATED).select_related("ticket")
        if wt.ticket is not None
    }
    return {overlay.get_dslr_tenant_for_variant(v) for v in variants if v}


def prune_dslr_snapshots_skipping(*, keep: int, in_use_tenants: set[str]) -> list[str]:
    """Prune DSLR snapshots (skipping in-use tenants) and return cleanup labels."""
    from teatree.utils.django_db import prune_dslr_snapshots  # noqa: PLC0415

    pruned = prune_dslr_snapshots(keep=keep, in_use_tenants=in_use_tenants)
    return [f"Pruned DSLR snapshot: {name}" for name in pruned]
