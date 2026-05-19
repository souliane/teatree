"""Record an :class:`OutboundClaim` on every successful outbound post (#1019).

Thin helper so the instrumented surfaces (``notify_user``, the review CLI,
the Notion writers) all record claims in a uniform shape. The
``loop.scanners.outbound_audit`` scanner reads from this single table to
verify every claim against the third-party API and DM the user on drift.

Recording is best-effort: a claim-record failure must never break the
publish path it is auditing. Every helper wraps the ORM write in
``transaction.atomic()`` and swallows ``IntegrityError`` (a duplicate
``idempotency_key`` collapses to a no-op) and unexpected ``DatabaseError``
so an outage of the audit ledger does not cascade into the CLI turn.
"""

import logging
from typing import Any

from django.db import DatabaseError, IntegrityError, transaction

from teatree.core.models import OutboundClaim
from teatree.core.session_identity import current_session_id as _resolve_agent_session_id

logger = logging.getLogger(__name__)


def record_claim(
    *,
    kind: "OutboundClaim.Kind | str",
    idempotency_key: str,
    target_url: str = "",
    agent_session_id: str = "",
    extra: dict[str, Any] | None = None,
) -> "OutboundClaim | None":
    """Record one outbound claim, returning the row or ``None`` on failure.

    Idempotent on ``idempotency_key`` — a retried publish with the same key
    no-ops and returns the existing row. Never raises into the caller:
    integrity races, DB outages, and bootstrap-before-Django errors all
    degrade to ``None`` + a logger warning. The caller's publish has
    already succeeded by the time we get here; failing to audit it must
    not turn that success into a user-visible failure.
    """
    kind_value = OutboundClaim.Kind(kind) if not isinstance(kind, OutboundClaim.Kind) else kind
    session_id = agent_session_id or _resolve_agent_session_id()
    try:
        with transaction.atomic():
            claim, _created = OutboundClaim.objects.get_or_create(
                idempotency_key=idempotency_key,
                defaults={
                    "kind": kind_value.value,
                    "target_url": target_url,
                    "agent_session_id": session_id,
                    "extra": extra or {},
                },
            )
    except IntegrityError:
        logger.debug("record_claim race on key=%s — already audited", idempotency_key)
        try:
            return OutboundClaim.objects.filter(idempotency_key=idempotency_key).first()
        except Exception:  # noqa: BLE001 — refetch is best-effort
            return None
    except DatabaseError as exc:
        logger.warning("record_claim DB failure for key=%s: %s", idempotency_key, exc)
        return None
    except Exception as exc:  # noqa: BLE001 — must never break the publish path
        logger.debug("record_claim unexpected failure for key=%s: %s", idempotency_key, exc)
        return None
    return claim


__all__ = ["_resolve_agent_session_id", "record_claim"]
