import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from teatree.core.github_app import installation_sync, webhook_normalize
from teatree.core.github_app.event_identity import entity_key_and_marker
from teatree.core.models import IncomingEvent, WebhookRejection
from teatree.core.views._rate_limit import webhook_rate_limiter
from teatree.core.views._webhook_persistence import persist_incoming_event

logger = logging.getLogger(__name__)

#: Guard against an unbounded request body before it is ever HMAC-verified or
#: parsed. Not a Django setting declaration (see ``known_unresolved_refs.yaml``
#: convention for ``TEATREE_GITHUB_WEBHOOK_SECRET``) — set via the environment,
#: default sized generously above a typical ``push`` event's commit list.
_DEFAULT_MAX_WEBHOOK_PAYLOAD_BYTES = 5_000_000

_INSTALLATION_EVENTS = frozenset({"installation", "installation_repositories"})


@method_decorator(csrf_exempt, name="dispatch")
class GitHubWebhookView(View):
    """Receiver for GitHub webhooks (#654 phase 6, App transport #4795).

    Verifies ``X-Hub-Signature-256`` against the ``TEATREE_GITHUB_WEBHOOK_SECRET``
    Django setting (HMAC-SHA256 of the raw body). ``X-GitHub-Delivery`` is
    GitHub's per-event UUID, used as a fallback idempotency key when
    :func:`~teatree.core.github_app.event_identity.identity_for` cannot derive a
    transport-independent one from the payload (see
    :mod:`teatree.core.github_app.webhook_normalize`). Every rejection —
    oversized, unsigned/bad signature, unconfigured secret, rate-limited, stale
    replay — is recorded to :class:`~teatree.core.models.github_app.WebhookRejection`
    before the request is refused; none of them ever reach persistence.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        secret = getattr(settings, "TEATREE_GITHUB_WEBHOOK_SECRET", "") or ""
        if not secret:
            logger.warning("GitHub webhook rejected: signing secret not configured")
            self._reject(WebhookRejection.Reason.NO_SECRET, request)
            return HttpResponse(status=503)

        if self._oversized(request):
            self._reject(WebhookRejection.Reason.OVERSIZED, request)
            return HttpResponse(status=413)

        if not self._authenticated(request, secret=secret):
            self._reject(WebhookRejection.Reason.SIGNATURE_INVALID, request)
            return HttpResponse(status=401)

        if not webhook_rate_limiter().allow(IncomingEvent.Source.GITHUB):
            logger.warning("GitHub webhook throttled — per-source rate limit exceeded")
            self._reject(WebhookRejection.Reason.RATE_LIMITED, request)
            return HttpResponse(status=429)

        payload = json.loads(request.body or b"{}")
        event_type = request.headers.get("X-GitHub-Event", "")
        delivery = request.headers.get("X-GitHub-Delivery", "") or hashlib.sha256(request.body or b"").hexdigest()[:16]

        if event_type in _INSTALLATION_EVENTS:
            installation_sync.handle(event_type, payload)

        if self._is_stale_replay(event_type, payload):
            self._reject(WebhookRejection.Reason.STALE_REPLAY, request, delivery=delivery)
            return HttpResponse(status=200)

        record = webhook_normalize.normalize(event_type, payload, delivery_id=delivery)
        persist_incoming_event(record)
        return HttpResponse(status=200)

    def _authenticated(self, request: HttpRequest, *, secret: str) -> bool:
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not signature.startswith("sha256="):
            return False
        digest = hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(f"sha256={digest}", signature)

    def _oversized(self, request: HttpRequest) -> bool:
        max_bytes = int(getattr(settings, "TEATREE_MAX_WEBHOOK_PAYLOAD_BYTES", _DEFAULT_MAX_WEBHOOK_PAYLOAD_BYTES))
        declared = request.headers.get("content-length") or ""
        if declared.isdigit() and int(declared) > max_bytes:
            return True
        return len(request.body or b"") > max_bytes

    def _is_stale_replay(self, event_type: str, payload: dict) -> bool:
        """True iff a NEWER update for the same entity was already ingested.

        Compares markers only when the candidate's ``entity_key`` matches a
        prior delivery's — an unrecognised family (no derivable identity) is
        never treated as stale, since there is nothing to compare it against.
        """
        found = entity_key_and_marker(event_type, payload)
        if found is None:
            return False
        entity_key, marker = found
        prior = IncomingEvent.objects.filter(
            source=IncomingEvent.Source.GITHUB,
            idempotency_key__startswith=f"{entity_key}:",
        ).first()
        if prior is None:
            return False
        prior_found = entity_key_and_marker(event_type, prior.payload_json)
        if prior_found is None or prior_found[0] != entity_key:
            return False
        return marker <= prior_found[1]

    def _reject(self, reason: str, request: HttpRequest, *, delivery: str = "") -> None:
        delivery_id = delivery or request.headers.get("X-GitHub-Delivery", "")
        WebhookRejection.record(source=IncomingEvent.Source.GITHUB, reason=reason, delivery_id=delivery_id)
