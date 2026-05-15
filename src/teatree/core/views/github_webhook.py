import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from teatree.core.models import IncomingEvent
from teatree.core.views._webhook_persistence import IngestionRecord, persist_incoming_event

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class GitHubWebhookView(View):
    """Receiver for GitHub webhooks (#654 phase 6).

    Verifies ``X-Hub-Signature-256`` against
    ``settings.TEATREE_GITHUB_WEBHOOK_SECRET`` (HMAC-SHA256 of the raw body).
    ``X-GitHub-Delivery`` is GitHub's per-event UUID — we use it as the
    idempotency key so retries collapse onto the same row.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        secret = getattr(settings, "TEATREE_GITHUB_WEBHOOK_SECRET", "") or ""
        if not secret:
            logger.warning("GitHub webhook rejected: signing secret not configured")
            return HttpResponse(status=503)

        if not self._authenticated(request, secret=secret):
            return HttpResponse(status=401)

        payload = json.loads(request.body or b"{}")
        delivery = request.headers.get("X-GitHub-Delivery", "") or hashlib.sha256(request.body or b"").hexdigest()[:16]
        idempotency_key = f"github:{delivery}"

        actor = (payload.get("sender") or {}).get("login") or (payload.get("review") or {}).get("user", {}).get(
            "login",
            "",
        )
        channel_ref = (payload.get("repository") or {}).get("full_name") or ""
        pr = payload.get("pull_request") or {}
        thread_ref = str(pr.get("number") or "")
        body_text = pr.get("title") or payload.get("action") or ""

        persist_incoming_event(
            IngestionRecord(
                source=IncomingEvent.Source.GITHUB,
                idempotency_key=idempotency_key,
                actor=actor,
                channel_ref=channel_ref,
                thread_ref=thread_ref,
                body=body_text,
                payload_json=payload,
            ),
        )
        return HttpResponse(status=200)

    def _authenticated(self, request: HttpRequest, *, secret: str) -> bool:
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not signature.startswith("sha256="):
            return False
        digest = hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(f"sha256={digest}", signature)
