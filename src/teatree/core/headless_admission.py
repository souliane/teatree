"""Headless (django-tasks) admission composition — the mirror of the claim gate (#4834).

:func:`teatree.core.managers_task_claim.claim_admission_block_reason` is the ONE
admission composition the interactive claim paths (``claimable``,
``claim_next_pending``) share. The headless django-tasks dispatch path — the
``post_save`` auto-enqueue, the drain-queue safety net, ``execute_task``'s own
claim, and the stuck-ticket auto-repair sweep — never asked it, so a frozen
factory (``worker_quiescing``, or a mode/hold that masks the ``dispatch`` loop
off) had zero effect there: only the loop-driven claim path was ever gated.

This composition cannot simply be ADDED to ``claim_admission_block_reason`` itself:
``teatree.loops.enable_verdict`` (the ``dispatch``-loop mask read below) transitively
depends on ``teatree.core.models`` -> ``teatree.core.managers`` ->
``teatree.core.managers_task_claim``, so a reverse edge from that leaf would cycle
(``forbid_circular_dependencies``, tach.toml). This module sits one layer up — the
bare ``teatree.core`` package already declares both edges — and composes the two
without disturbing the leaf's own dependency-free shape.
"""

from teatree.core.managers_task_claim import claim_admission_block_reason


def headless_admission_block_reason() -> str:
    """Why NO headless (django-tasks) admission may proceed right now, or ``""``.

    Every headless dispatch site calls this rather than restating the boolean, so
    the interactive and headless paths can never drift on what "the factory is
    frozen" means — the DRY invariant ``claim_admission_block_reason`` itself
    documents, extended to the one direction it structurally cannot reach.
    """
    reason = claim_admission_block_reason()
    if reason:
        return reason
    from teatree.loops.enable_verdict import loop_admits  # noqa: PLC0415 — deferred: heavier ORM-backed import

    if not loop_admits("dispatch"):
        return "the dispatch loop is masked off by the active mode/hold"
    return ""


__all__ = ["headless_admission_block_reason"]
