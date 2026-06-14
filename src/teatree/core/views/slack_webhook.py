import hashlib
import hmac
import json
import logging
import time

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from teatree.core.models import IncomingEvent
from teatree.core.views._rate_limit import webhook_rate_limiter
from teatree.core.views._webhook_persistence import IngestionRecord, persist_incoming_event

logger = logging.getLogger(__name__)

REPLAY_WINDOW_SECONDS = 5 * 60


@method_decorator(csrf_exempt, name="dispatch")
class SlackWebhookView(View):
    """Receiver for Slack's Events API (#654 phase 1).

    Verifies the request via HMAC + replay window, then persists an
    `IncomingEvent` keyed by the Slack event id so retries are idempotent.
    Returns 200 fast — downstream processing happens on the next loop tick.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        secret = getattr(settings, "TEATREE_SLACK_SIGNING_SECRET", "") or ""
        if not secret:
            logger.warning("Slack webhook rejected: signing secret not configured")
            return HttpResponse(status=503)

        if not self._authenticated(request, secret=secret):
            return HttpResponse(status=401)

        payload = json.loads(request.body or b"{}")
        if payload.get("type") == "url_verification":
            return JsonResponse({"challenge": payload.get("challenge", "")})

        if not webhook_rate_limiter().allow(IncomingEvent.Source.SLACK):
            logger.warning("Slack webhook throttled — per-source rate limit exceeded")
            return HttpResponse(status=429)

        self._persist(payload, body=request.body)
        return HttpResponse(status=200)

    def _authenticated(self, request: HttpRequest, *, secret: str) -> bool:
        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")
        if not timestamp or not signature:
            return False
        try:
            ts_int = int(timestamp)
        except ValueError:
            return False
        if abs(time.time() - ts_int) > REPLAY_WINDOW_SECONDS:
            return False
        basestring = b"v0:" + timestamp.encode() + b":" + request.body
        digest = hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
        return hmac.compare_digest(f"v0={digest}", signature)

    def _persist(self, payload: dict, *, body: bytes) -> None:
        event = payload.get("event") or {}
        event_id = payload.get("event_id") or ""
        idempotency_key = f"slack:{event_id}" if event_id else f"slack:{hashlib.sha256(body).hexdigest()[:16]}"
        persist_incoming_event(
            IngestionRecord(
                source=IncomingEvent.Source.SLACK,
                idempotency_key=idempotency_key,
                actor=event.get("user", "") or "",
                channel_ref=event.get("channel", "") or "",
                thread_ref=event.get("thread_ts", "") or event.get("ts", "") or "",
                parent_ts=_reply_parent_ts(event),
                body=event.get("text", "") or "",
                payload_json=payload,
            ),
        )


def _reply_parent_ts(event: dict) -> str:
    """The parent/root ts of a thread reply, or ``""`` for a root message (#2230).

    A Slack message is a reply iff it carries a ``thread_ts`` that differs
    from its own ``ts`` — the root of a thread has ``thread_ts == ts``, and
    a standalone message has no ``thread_ts`` at all. The parent text is not
    in the reply payload; the loop scanner resolves and persists it.
    """
    thread_ts = event.get("thread_ts", "") or ""
    ts = event.get("ts", "") or ""
    return thread_ts if thread_ts and thread_ts != ts else ""
