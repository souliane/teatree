from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass

from django.db import transaction
from django.db.models import F

from teatree.core.backend_protocols import MessagingBackend, MessagingReadRefusedError
from teatree.core.forge_url import is_forge_url
from teatree.core.models import ThreadBinding, Ticket, TicketDmPost, TicketDmThread
from teatree.types import RawAPIDict

_DELETED_ROOT_CODES = frozenset({"thread_not_found", "message_not_found"})


@dataclass(frozen=True, slots=True)
class TicketDmRequest:
    backend: MessagingBackend
    user_id: str
    text: str
    blocks: list[RawAPIDict] | None
    ticket_id: int
    deliver: Callable[..., tuple[str, str, str]]
    question_id: int | None = None
    fallback_thread_ts: str = ""


def root_is_deleted(backend: MessagingBackend, channel: str, root_ts: str) -> bool:
    try:
        messages = backend.fetch_thread_replies(channel=channel, thread_ts=root_ts)
    except MessagingReadRefusedError as exc:
        return exc.error_code in _DELETED_ROOT_CODES
    except (OSError, RuntimeError, ValueError):
        return False
    root = next((message for message in messages if str(message.get("ts", "")) == root_ts), None)
    return bool(root and root.get("subtype") == "tombstone")


def _retire(thread: TicketDmThread) -> None:
    TicketDmThread.objects.filter(pk=thread.pk, root_ts=thread.root_ts).update(
        root_ts="", generation=F("generation") + 1
    )


def _record_root(thread: TicketDmThread, ticket: Ticket, channel: str, question_id: int | None = None) -> None:
    ThreadBinding.bind(ticket=ticket, channel=channel, thread_ts=thread.root_ts)
    TicketDmPost.objects.get_or_create(
        channel=channel,
        slack_ts=thread.root_ts,
        defaults={"thread": thread, "generation": thread.generation, "question_id": question_id},
    )


@dataclass(slots=True)
class _RootSend:
    request: TicketDmRequest
    ticket: Ticket
    stack: ExitStack
    thread: TicketDmThread | None = None
    first_send: bool = False

    def root_for_channel(self, channel: str) -> str:
        self.thread, _ = TicketDmThread.objects.get_or_create(ticket=self.ticket)
        if self.thread.channel and self.thread.channel != channel:
            msg = "ticket DM channel changed"
            raise RuntimeError(msg)
        if self.thread.root_ts and root_is_deleted(self.request.backend, channel, self.thread.root_ts):
            _retire(self.thread)
            self.thread.refresh_from_db()
        if self.thread.root_ts:
            _record_root(self.thread, self.ticket, channel)
            return self.thread.root_ts
        self.stack.enter_context(transaction.atomic())
        TicketDmThread.objects.filter(ticket=self.ticket).update(generation=F("generation"))
        self.thread = TicketDmThread.objects.select_for_update().get(ticket=self.ticket)
        if self.thread.channel and self.thread.channel != channel:
            msg = "ticket DM channel changed"
            raise RuntimeError(msg)
        self.first_send = not bool(self.thread.root_ts)
        if not self.first_send:
            _record_root(self.thread, self.ticket, channel)
        return self.thread.root_ts


def deliver_ticket_dm(request: TicketDmRequest) -> tuple[str, str, str]:
    ticket = Ticket.objects.filter(pk=request.ticket_id).first()
    if ticket is None or (ticket.issue_url and not is_forge_url(ticket.issue_url)):
        return request.deliver(
            request.backend,
            user_id=request.user_id,
            text=request.text,
            blocks=request.blocks,
            thread_ts=request.fallback_thread_ts,
        )
    with ExitStack() as stack:
        send = _RootSend(request, ticket, stack)
        channel, posted_ts, failure = request.deliver(
            request.backend,
            user_id=request.user_id,
            text=request.text,
            blocks=request.blocks,
            thread_ts=send.root_for_channel,
        )
        thread = send.thread
        if failure:
            if (
                thread is not None
                and thread.root_ts
                and root_is_deleted(request.backend, thread.channel, thread.root_ts)
            ):
                _retire(thread)
            return channel, posted_ts, failure
        if thread is None:
            return channel, posted_ts, ""
        if send.first_send:
            thread.channel = channel
            thread.root_ts = posted_ts
            thread.save(update_fields=["channel", "root_ts"])
            _record_root(thread, ticket, channel, request.question_id)
        else:
            TicketDmPost.objects.get_or_create(
                channel=channel,
                slack_ts=posted_ts,
                defaults={"thread": thread, "generation": thread.generation, "question_id": request.question_id},
            )
        return channel, posted_ts, ""


def record_ticket_thread_post(channel: str, root_ts: str, post_ts: str) -> None:
    if not post_ts:
        return
    thread = TicketDmThread.objects.filter(channel=channel, root_ts=root_ts).first()
    if thread is not None:
        TicketDmPost.objects.get_or_create(
            channel=channel,
            slack_ts=post_ts,
            defaults={"thread": thread, "generation": thread.generation},
        )
