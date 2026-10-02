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
import os
from typing import Any

from django.db import DatabaseError, IntegrityError, transaction

from teatree.core.models import OutboundClaim
from teatree.core.session_identity import current_session_id as _resolve_agent_session_id

logger = logging.getLogger(__name__)


def _active_overlay_name() -> str:
    """Read the active overlay name from the env (``T3_OVERLAY_NAME``).

    Empty string is the canonical single-overlay default — record helpers
    stamp that on the claim's ``extra["overlay"]`` so the audit scanner
    knows which credential pipeline to re-read with (#1275).
    """
    return os.environ.get("T3_OVERLAY_NAME", "") or ""


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

    Stamps ``extra["overlay"]`` from ``T3_OVERLAY_NAME`` so the audit
    verifier can re-read the artifact through THAT overlay's credentials —
    not whichever credential a process-global resolver lands on (#1275).
    Callers that already have an explicit overlay in mind pass it in the
    ``extra`` dict directly (``extra={"overlay": "client-A", ...}``); that
    value wins over the env-var fallback.
    """
    kind_value = OutboundClaim.Kind(kind) if not isinstance(kind, OutboundClaim.Kind) else kind
    session_id = agent_session_id or _resolve_agent_session_id()
    final_extra: dict[str, Any] = dict(extra or {})
    final_extra.setdefault("overlay", _active_overlay_name())
    try:
        with transaction.atomic():
            claim, _created = OutboundClaim.objects.get_or_create(
                idempotency_key=idempotency_key,
                defaults={
                    "kind": kind_value.value,
                    "target_url": target_url,
                    "agent_session_id": session_id,
                    "extra": final_extra,
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
