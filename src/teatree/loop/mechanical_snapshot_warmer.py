"""Snapshot-warmer mechanical handler — the executor for ``snapshot_warmer.refresh_needed`` (souliane/teatree#2949).

The scanner (:mod:`teatree.loop.scanners.snapshot_warmer`) only FLAGS a stale
reference DB; this module does the actual (slow) restore+migrate+snapshot
work, mirroring the detect/execute split every other mechanical scanner uses
(``mechanical_resources.free_resources``, ``mechanical_local_stack``).
Best-effort: any failure logs and is swallowed so a bad tick never aborts the
loop.
"""

import logging

from teatree.loop.dispatch import ActionPayload

logger = logging.getLogger(__name__)


def refresh_snapshot(payload: ActionPayload) -> None:
    """Refresh the reference DB named in *payload* — best-effort, never raises into the loop."""
    cfg = payload.get("config")
    if cfg is None:
        logger.warning("refresh_snapshot: no config in payload — nothing to do")
        return
    try:
        from teatree.utils.django_db_snapshot_warmer import refresh_reference_snapshot  # noqa: PLC0415

        ok = refresh_reference_snapshot(cfg)
    except Exception:
        logger.exception("refresh_snapshot: refresh failed for %r", getattr(cfg, "ref_db_name", cfg))
        return
    if ok:
        logger.info("refresh_snapshot: %s is current", getattr(cfg, "ref_db_name", cfg))
    else:
        logger.warning("refresh_snapshot: %s refresh did not succeed", getattr(cfg, "ref_db_name", cfg))


__all__ = ["refresh_snapshot"]
