"""Helpers for ``t3 teatree workspace`` subcommands (#1306, #1310).

Split from :mod:`workspace` to keep the command module under the per-
module LOC cap. Covers the DSLR-snapshot-in-use guard shared by
``clean-all``, the variant-mismatch refusal used by ``ticket``, the
overlay-name resolution helper that ``ticket`` leans on when
``T3_OVERLAY_NAME`` is missing on a multi-overlay install, and the
interrupted-provision DB heal used by ``start`` (#1038).
"""

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, TypedDict

from teatree.core.gates.orphan_guard import find_orphans_in_workspace
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import get_overlay, infer_overlay_for_url
from teatree.core.readiness import run_and_report_probes
from teatree.core.runners import heal_missing_provisioned_db

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase


def report_worktree_probes(
    worktrees: list[Worktree],
    overlay: "OverlayBase",
    write: Callable[[str], None],
    *,
    note_empty: bool,
) -> tuple[int, int]:
    """Run each worktree's readiness probes; return ``(total, failures)``.

    Shared by ``start`` (probe only the worktrees that started) and
    ``ready`` (probe every worktree). ``note_empty`` reports a worktree
    with no probes explicitly (``ready``) or skips it silently (``start``).
    """
    total = 0
    total_failures = 0
    for wt in worktrees:
        probes = overlay.get_readiness_probes(wt)
        if not probes:
            if note_empty:
                write(f"  {wt.repo_path}: no probes")
            continue
        write(f"  {wt.repo_path}:")
        summary = run_and_report_probes(probes, write_line=write, indent="    ")
        total += summary.total
        total_failures += summary.failures
    return total, total_failures


class OrphanEntry(TypedDict):
    repo: str
    branch: str
    status: str
    ahead_count: int


def list_orphan_entries() -> list["OrphanEntry"]:
    """JSON-serialisable orphan-branch list (commits ahead of origin/main, no open PR)."""
    return [
        OrphanEntry(repo=r.repo, branch=r.branch, status=r.status.value, ahead_count=r.ahead_count)
        for r in find_orphans_in_workspace()
    ]


def warn_orphans(write: Callable[[str], None]) -> None:
    """Warn (up to 5 previewed) about orphan branches before a session-closing action."""
    orphans = find_orphans_in_workspace()
    if not orphans:
        return
    preview = orphans[:5]
    write(f"WARNING: {len(orphans)} orphan branch(es) in the workspace:")
    for r in preview:
        write(f"  - {r.repo} ({r.branch}, {r.ahead_count} ahead, {r.status.value})")
    if len(orphans) > len(preview):
        write(f"  - …and {len(orphans) - len(preview)} more")
    write(
        "Run `t3 <overlay> pr ensure-pr --branch <name>` to track them, "
        "or `t3 <overlay> workspace clean-all` to reap synced ones.",
    )


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


def resolve_overlay_name_for_url(issue_url: str) -> str | None:
    """Pick an overlay name for an issue URL with no explicit env var (#1310).

    Precedence: ``T3_OVERLAY_NAME`` env var wins when set (the CLI bridge
    sets it from ``t3 <overlay>`` invocations and ``get_overlay`` consumes
    it on a ``None`` argument; this helper returns ``None`` then to defer).
    Otherwise, route through ``infer_overlay_for_url`` — every registered
    overlay declares its workspace repo slugs; the first that owns
    ``issue_url`` wins. Returns ``None`` if no overlay claims the URL, in
    which case ``get_overlay(None)`` raises ``ImproperlyConfigured`` with
    the actual list of installed overlays so the user knows to pass
    ``T3_OVERLAY_NAME`` explicitly.
    """
    if os.environ.get("T3_OVERLAY_NAME"):
        return None  # let ``get_overlay`` consume the env var
    inferred = infer_overlay_for_url(issue_url)
    return inferred or None


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


def heal_db_or_record_failure(
    wt: Worktree,
    overlay: "OverlayBase",
    failures: list[str],
    write_out: Callable[[str], object],
) -> bool:
    """Heal a sibling worktree whose interrupted provision left no DB (#1038).

    Wraps :func:`teatree.core.runners.heal_missing_provisioned_db` for the
    multi-repo ``workspace start`` loop: a re-provision is reported, a heal
    failure is recorded against ``failures`` (per-worktree isolation — one bad
    repo never aborts the whole ticket). Returns ``True`` when the caller should
    SKIP starting this worktree (its heal failed), ``False`` to proceed.
    """
    try:
        if heal_missing_provisioned_db(wt, overlay):
            write_out(f"  Re-provisioned missing DB for {wt.repo_path} before start.")
    except RuntimeError as exc:
        write_out(f"  {exc}")
        failures.append(wt.repo_path)
        return True
    return False
