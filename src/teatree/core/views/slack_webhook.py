import hashlib
import hmac
import json
import logging
import time

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from teatree.core.models import IncomingEvent

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

        self._persist(payload)
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

    def _persist(self, payload: dict) -> None:
        event = payload.get("event") or {}
        event_id = payload.get("event_id") or ""
        idempotency_key = f"slack:{event_id}" if event_id else f"slack:{int(time.time() * 1000)}"
        try:
            with transaction.atomic():
                IncomingEvent.objects.create(
                    source=IncomingEvent.Source.SLACK,
                    actor=event.get("user", "") or "",
                    channel_ref=event.get("channel", "") or "",
                    thread_ref=event.get("thread_ts", "") or event.get("ts", "") or "",
                    body=event.get("text", "") or "",
                    payload_json=payload,
                    idempotency_key=idempotency_key,
                )
        except IntegrityError:
            logger.debug("Slack event %s already ingested — replay suppressed", idempotency_key)
