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
from teatree.core.views._rate_limit import webhook_rate_limiter
from teatree.core.views._webhook_persistence import IngestionRecord, persist_incoming_event

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class GitLabWebhookView(View):
    """Receiver for GitLab project / system webhooks (#654 phase 6).

    GitLab does not sign payloads — it sends a project-configured shared
    secret in ``X-Gitlab-Token``. We compare it to
    ``settings.TEATREE_GITLAB_WEBHOOK_TOKEN`` and persist an
    ``IncomingEvent`` keyed by a hash of the body so retries are idempotent.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        secret = getattr(settings, "TEATREE_GITLAB_WEBHOOK_TOKEN", "") or ""
        if not secret:
            logger.warning("GitLab webhook rejected: token not configured")
            return HttpResponse(status=503)

        provided = request.headers.get("X-Gitlab-Token", "")
        if not provided or not hmac.compare_digest(provided, secret):
            return HttpResponse(status=401)

        if not webhook_rate_limiter().allow(IncomingEvent.Source.GITLAB):
            logger.warning("GitLab webhook throttled — per-source rate limit exceeded")
            return HttpResponse(status=429)

        payload = json.loads(request.body or b"{}")
        body_hash = hashlib.sha256(request.body or b"").hexdigest()[:16]
        idempotency_key = f"gitlab:{body_hash}"

        actor = (payload.get("user") or {}).get("username") or ""
        channel_ref = (payload.get("project") or {}).get("path_with_namespace") or ""
        attrs = payload.get("object_attributes") or {}
        thread_ref = str(attrs.get("iid") or "")
        body_text = attrs.get("title") or payload.get("object_kind") or ""

        persist_incoming_event(
            IngestionRecord(
                source=IncomingEvent.Source.GITLAB,
                idempotency_key=idempotency_key,
                actor=actor,
                channel_ref=channel_ref,
                thread_ref=thread_ref,
                body=body_text,
                payload_json=payload,
            ),
        )
        return HttpResponse(status=200)
