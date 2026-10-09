"""Snapshot-warmer mechanical handler — the executor for ``snapshot_warmer.refresh_needed`` (souliane/teatree#2949).

The scanner (:mod:`teatree.loop.scanners.snapshot_warmer`) only FLAGS a stale
reference DB; this module does the actual (slow) restore+migrate+snapshot
work, mirroring the detect/execute split every other mechanical scanner uses
(``mechanical_resources.free_resources``, ``mechanical_local_stack``).
A failure raises into ``_execute_mechanical``, which records it in the tick's errors.
"""

import logging

from teatree.loop.dispatch import ActionPayload

logger = logging.getLogger(__name__)


def refresh_snapshot(payload: ActionPayload) -> None:
    """Refresh the reference DB named in *payload*; a failure raises into the tick's errors."""
    cfg = payload.get("config")
    if cfg is None:
        logger.warning("refresh_snapshot: no config in payload — nothing to do")
        return
    from teatree.utils.django_db.snapshot_warmer import (  # noqa: PLC0415 — deferred: loaded at tick time, not import
        refresh_reference_snapshot,
    )

    ok = refresh_reference_snapshot(cfg)
    if ok:
        logger.info("refresh_snapshot: %s is current", getattr(cfg, "ref_db_name", cfg))
    else:
        logger.warning("refresh_snapshot: %s refresh did not succeed", getattr(cfg, "ref_db_name", cfg))


__all__ = ["refresh_snapshot"]
