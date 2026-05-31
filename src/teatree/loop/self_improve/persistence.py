"""Durable ``SelfImproveFiring`` row ops (BLUEPRINT § 5.7).

Mirrors the style of ``loop/persistence.py``: a thin functional surface
that translates detector reports into DB writes through
``transaction.atomic()``.  The dedup contract is enforced by the unique
constraint on ``(detector, dedup_key)``; this module handles the read +
update side (record a new firing, count recent Slack firings, look up
the last firing for a key).
"""

import datetime as dt
import logging

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from teatree.core.models.self_improve_firing import SelfImproveFiring
from teatree.loop.self_improve.detectors.base import DetectorReport

logger = logging.getLogger(__name__)


# Global Slack rate cap (one self-improve DM per 30 min, regardless of
# detector). The integration with `actions.format_slack_payload` consults
# `recent_slack_firings_within(SLACK_RATE_CAP_SECONDS)` and downgrades
# the rung to ``statusline`` when the cap is hit.
SLACK_RATE_CAP_SECONDS = 30 * 60


def latest_firing(detector: str, dedup_key: str) -> SelfImproveFiring | None:
    """Return the persisted firing for ``(detector, dedup_key)`` or ``None``."""
    return SelfImproveFiring.objects.filter(detector=detector, dedup_key=dedup_key).first()


def record_firing(
    report: DetectorReport,
    *,
    action: str,
    now: dt.datetime | None = None,
) -> SelfImproveFiring:
    """Insert or update the durable firing row for ``report``.

    New row: ``get_or_create`` with ``action_count=1``.  Existing row: a
    single ``filter().update(action_count=F(...)+1)`` so the increment is
    evaluated by the DB and concurrent ticks cannot lose-update the counter.
    ``action_count`` increments on every observation — the monotonic counter
    is the post-hoc telemetry signal.
    """
    moment = now or timezone.now()
    with transaction.atomic():
        firing, created = SelfImproveFiring.objects.get_or_create(
            detector=report.detector,
            dedup_key=report.dedup_key,
            defaults={
                "state_hash": report.state_hash,
                "severity": report.severity,
                "first_fired_at": moment,
                "last_fired_at": moment,
                "last_action": action,
                "payload": report.payload,
                "action_count": 1,
            },
        )
        if not created:
            SelfImproveFiring.objects.filter(pk=firing.pk).update(
                state_hash=report.state_hash,
                severity=report.severity,
                last_fired_at=moment,
                last_action=action,
                payload=report.payload,
                action_count=F("action_count") + 1,
            )
            firing.refresh_from_db()
    return firing


def recent_slack_firings_within(seconds: int, *, now: dt.datetime | None = None) -> int:
    """Count Slack-rung firings inside the trailing ``seconds`` window.

    Used by ``actions.format_slack_payload`` to enforce the global rate
    cap — the count is detector-agnostic by design (one cap across the
    whole monitor).
    """
    moment = now or timezone.now()
    cutoff = moment - dt.timedelta(seconds=seconds)
    return SelfImproveFiring.objects.filter(
        last_action=SelfImproveFiring.Action.SLACK,
        last_fired_at__gte=cutoff,
    ).count()
