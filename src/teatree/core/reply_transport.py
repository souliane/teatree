"""Reply transport — the outbound half of the autonomous-events loop (#654 phase 4).

Every place teatree posts on behalf of the user (Slack thread reply,
GitLab MR comment, GitHub PR comment) goes through a ``Replier``
implementation so the audit trail in ``ReplyDispatch`` is canonical and
idempotency keys are enforced. The default ``NoopReplier`` records the
dispatch as ``sent`` without performing any network I/O — production
Slack/GitLab/GitHub repliers wrap the same internal send hook once their
respective backends are wired in (next PR).
"""

import logging
from dataclasses import dataclass
from typing import Protocol

from django.db import IntegrityError, transaction

from teatree.core.models import IncomingEvent, ReplyDispatch

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ReplySpec:
    event: IncomingEvent
    target_ref: str
    body: str
    idempotency_key: str
    action_name: str


class Replier(Protocol):
    def post_in_thread(
        self,
        *,
        event: IncomingEvent,
        target_ref: str,
        thread_ref: str,
        body: str,
        idempotency_key: str,
    ) -> ReplyDispatch: ...

    def post_dm(
        self,
        *,
        event: IncomingEvent,
        actor: str,
        body: str,
        idempotency_key: str,
    ) -> ReplyDispatch: ...

    def post_comment(
        self,
        *,
        event: IncomingEvent,
        target_ref: str,
        body: str,
        idempotency_key: str,
    ) -> ReplyDispatch: ...


class NoopReplier:
    """Reply transport that records but does not send.

    The default for development and tests. Production replier subclasses
    delegate the actual API call to a platform backend in ``_send()`` and
    then update the recorded ``ReplyDispatch`` with the outcome.
    """

    def post_in_thread(
        self,
        *,
        event: IncomingEvent,
        target_ref: str,
        thread_ref: str,
        body: str,
        idempotency_key: str,
    ) -> ReplyDispatch:
        composite_target = f"{target_ref}/{thread_ref}" if thread_ref else target_ref
        return self._send(
            ReplySpec(
                event=event,
                target_ref=composite_target,
                body=body,
                idempotency_key=idempotency_key,
                action_name="post_in_thread",
            ),
        )

    def post_dm(
        self,
        *,
        event: IncomingEvent,
        actor: str,
        body: str,
        idempotency_key: str,
    ) -> ReplyDispatch:
        return self._send(
            ReplySpec(
                event=event,
                target_ref=actor,
                body=body,
                idempotency_key=idempotency_key,
                action_name="post_dm",
            ),
        )

    def post_comment(
        self,
        *,
        event: IncomingEvent,
        target_ref: str,
        body: str,
        idempotency_key: str,
    ) -> ReplyDispatch:
        return self._send(
            ReplySpec(
                event=event,
                target_ref=target_ref,
                body=body,
                idempotency_key=idempotency_key,
                action_name="post_comment",
            ),
        )

    def _send(self, spec: ReplySpec) -> ReplyDispatch:
        logger.debug("%s swallowing %d-char body for %s", type(self).__name__, len(spec.body), spec.target_ref)
        try:
            with transaction.atomic():
                return ReplyDispatch.objects.create(
                    event=spec.event,
                    target_ref=spec.target_ref,
                    action_name=spec.action_name,
                    idempotency_key=spec.idempotency_key,
                    status=ReplyDispatch.Status.SENT,
                )
        except IntegrityError:
            logger.debug("Reply %s already recorded — idempotent no-op", spec.idempotency_key)
            return ReplyDispatch.objects.get(idempotency_key=spec.idempotency_key)
