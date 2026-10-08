import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from typing import Self

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from teatree.core.intake.github_payload import GitHubPayloadFields
from teatree.core.models import IncomingEvent
from teatree.core.views._webhook_persistence import IngestionRecord, persist_incoming_event
from teatree.core.views._webhook_secrets import WebhookSecretUnavailableError, webhook_secrets
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)

_DELIVERY = re.compile(r"[A-Za-z0-9-]{1,64}")
_EVENT = re.compile(r"[a-z_]{1,64}")
_TARGET_TYPE = re.compile(r"[a-z_]{1,32}")
_TARGET_ID = re.compile(r"[0-9]{1,20}")
_CONTENT_LENGTH = re.compile(r"[0-9]{1,20}")
_SIGNATURE = re.compile(r"sha256=[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class DeliveryHeaders:
    delivery: str
    event: str
    target: str

    @classmethod
    def from_request(cls, request: HttpRequest) -> Self | None:
        delivery = request.headers.get("X-GitHub-Delivery", "")
        event = request.headers.get("X-GitHub-Event", "")
        target_type = request.headers.get("X-GitHub-Hook-Installation-Target-Type", "")
        target_id = request.headers.get("X-GitHub-Hook-Installation-Target-ID", "")
        if not (
            _DELIVERY.fullmatch(delivery)
            and _EVENT.fullmatch(event)
            and _TARGET_TYPE.fullmatch(target_type)
            and _TARGET_ID.fullmatch(target_id)
        ):
            return None
        return cls(delivery=delivery, event=event, target=f"{target_type}-{target_id}")


@method_decorator(csrf_exempt, name="dispatch")
class GitHubWebhookView(View):
    """Receiver for GitHub webhooks, verified per webhook against the secret its target headers name.

    Rows are stored settled: no handler consumes GitHub events yet, and the generic drain would read a PR title
    as a question.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        headers = DeliveryHeaders.from_request(request)
        if headers is None:
            return HttpResponse(status=400)
        if (refusal := self._length_refusal(request, headers) or self._auth_refusal(request, headers)) is not None:
            return refusal
        payload = self._payload_object(request.body)
        if payload is None:
            return HttpResponse(status=400)
        fields = GitHubPayloadFields.from_payload(headers.event, payload)
        persist_incoming_event(
            IngestionRecord(
                source=IncomingEvent.Source.GITHUB,
                idempotency_key=f"github:{headers.delivery}",
                event_name=headers.event,
                actor=fields.actor,
                channel_ref=fields.channel_ref,
                thread_ref=fields.thread_ref,
                body=fields.body,
                payload_json=payload,
                settled=True,
            ),
        )
        return HttpResponse(status=200)

    @staticmethod
    def _length_refusal(request: HttpRequest, headers: DeliveryHeaders) -> HttpResponse | None:
        declared = request.headers.get("content-length") or ""
        if not declared:
            return HttpResponse(status=411)
        if not _CONTENT_LENGTH.fullmatch(declared):
            return HttpResponse(status=400)
        cap = settings.DATA_UPLOAD_MAX_MEMORY_SIZE
        if cap is not None and int(declared) > cap:
            logger.warning(
                "GitHub webhook delivery %s (%s) refused: declared length %s exceeds the %s-byte cap",
                headers.delivery,
                headers.event,
                declared,
                cap,
            )
            return HttpResponse(status=413)
        return None

    @staticmethod
    def _auth_refusal(request: HttpRequest, headers: DeliveryHeaders) -> HttpResponse | None:
        signature = request.headers.get("X-Hub-Signature-256", "")
        try:
            verified = _verified(headers.target, request.body, signature)
        except WebhookSecretUnavailableError as exc:
            logger.debug("GitHub webhook delivery %s (%s) answered 503: %s", headers.delivery, headers.event, exc)
            return HttpResponse(status=503)
        return None if verified else HttpResponse(status=401)

    @staticmethod
    def _payload_object(body: bytes) -> RawAPIDict | None:
        try:
            payload = json.loads(body)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None


def _verified(target: str, body: bytes, signature: str) -> bool:
    if not _SIGNATURE.fullmatch(signature):
        return False
    secrets = webhook_secrets()
    secret = secrets.secret_for(target)
    if secret is None:
        return False
    if _signs(secret, body, signature):
        return True
    refreshed = secrets.refreshed_secret(target)
    return refreshed is not None and _signs(refreshed, body, signature)


def _signs(secret: str, body: bytes, signature: str) -> bool:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={digest}", signature)
