"""Helpers for ``t3 workspace`` subcommands (#1306).

Split from :mod:`workspace` to keep the command module under the per-
module LOC cap. Covers two surfaces: the DSLR-snapshot-in-use guard
shared by ``clean-all`` and the variant-mismatch refusal used by
``ticket``.
"""

from collections.abc import Callable

from teatree.core.models import Ticket, Worktree
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


def reject_variant_mismatch(write_err: Callable[[str], None], ticket: Ticket, variant: str) -> None:
    """Refuse to rebind an existing ticket to a different variant (#1306).

    Pre-fix `workspace ticket <url> --variant <v>` silently kept the
    existing ticket's variant and rebound to the URL's inferred branch
    — downstream operations then targeted the wrong code. Raises
    `SystemExit(2)` with a remediation hint when the caller supplied a
    `--variant` that disagrees with the row's variant.
    """
    if variant and ticket.variant and variant != ticket.variant:
        write_err(
            f"  ticket #{ticket.ticket_number} already exists with variant {ticket.variant!r}; "
            f"refusing to rebind to variant {variant!r}. "
            f"Use `t3 <overlay> ticket switch` or create a new ticket scope."
        )
        raise SystemExit(2)
