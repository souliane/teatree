"""t3-master DB ``LoopLease`` reads for the hook gates (#2851).

Self-contained leaf (ZERO dependency on ``hook_router``, mirroring
``django_bootstrap``): the shrink-only ``hook_router`` god-module cannot grow,
so the t3-master DB-lease read concern — the skip-consult env knob and the
take-over reconcile read — lives here and is imported back into the router.
``_db_live_foreign_owner`` stays in the router (its tests patch the router's
``_db_lease_consult_disabled`` / ``bootstrap_teatree_django`` bindings).
"""

import os

from hooks.scripts.django_bootstrap import bootstrap_teatree_django

# Skips the ``LoopLease`` DB cross-check (and its ``django.setup()``);
# collapses to the same fail-open value an absent DB already yields.
_SKIP_DB_LEASE_CONSULT_ENV = "T3_LOOP_SKIP_DB_LEASE_CONSULT"


def db_lease_consult_disabled() -> bool:
    return os.environ.get(_SKIP_DB_LEASE_CONSULT_ENV) == "1"


def db_owner_is_current_session(session_id: str) -> bool:
    """Whether the LIVE DB ``t3-master`` lease is held by ``session_id`` (#2851).

    The take-over reconcile READ. ``t3 loop claim --take-over`` writes ONLY the
    DB ``LoopLease`` row, never the ``_OWNER_LOOP`` file registry, so a displaced
    owner still alive in the file registry would otherwise make
    ``_claim_loop_ownership`` back off without seeing the hand-off. A POSITIVE
    DB-lease match (not merely "no live foreign DB owner") is required so a foreign
    file owner is reconciled only on an explicit DB take-over BY this session — an
    unowned or foreign DB lease never lets us steal a live foreign owner. The
    comparison is exact-string on the unmodified session id. Mirrors the
    ``_db_live_foreign_owner`` disabled / bootstrap / fail-open envelope: any DB /
    import error returns ``False`` so a hiccup never falsely grants ownership.
    """
    if not session_id:
        return False
    if db_lease_consult_disabled():
        return False
    if not bootstrap_teatree_django():
        return False
    try:
        from teatree.core.loop_lease_manager import T3_MASTER_SLOT  # noqa: PLC0415 — deferred: cold-hook import
        from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry

        status = LoopLease.objects.ownership_status(T3_MASTER_SLOT)
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return False
    return status.is_live and status.owner_session == session_id
