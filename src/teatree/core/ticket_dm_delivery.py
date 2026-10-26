from collections.abc import Callable
from dataclasses import dataclass
from html import escape

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
    post_root: Callable[[MessagingBackend, str, str], RawAPIDict]
    question_id: int | None = None


def _header(ticket: Ticket) -> str:
    title = ticket.extra.get("issue_title") or ticket.short_description
    label = ticket.repo_namespaced_key or f"ticket-{ticket.pk}"
    ref = f"<{ticket.issue_url}|{escape(label, quote=False)}>" if is_forge_url(ticket.issue_url) else label
    return f"{ref} {escape(str(title), quote=False)}".strip()


def _root_messages(backend: MessagingBackend, channel: str, root_ts: str) -> list[RawAPIDict]:
    return backend.fetch_thread_replies(channel=channel, thread_ts=root_ts)


def _root_is_deleted(backend: MessagingBackend, channel: str, root_ts: str) -> bool:
    try:
        messages = _root_messages(backend, channel, root_ts)
    except MessagingReadRefusedError as exc:
        return exc.error_code in _DELETED_ROOT_CODES
    except (OSError, RuntimeError, ValueError):
        return False
    root = next((message for message in messages if str(message.get("ts", "")) == root_ts), None)
    return bool(root and root.get("subtype") == "tombstone")


def _retire(thread: TicketDmThread) -> None:
    thread.root_ts = ""
    thread.generation += 1
    thread.save(update_fields=["root_ts", "generation"])


def _open_header(request: TicketDmRequest, ticket: Ticket, thread: TicketDmThread, channel: str) -> None:
    response = request.post_root(request.backend, channel, _header(ticket))
    root_ts = str(response.get("ts", "")) if isinstance(response, dict) else ""
    if not isinstance(response, dict) or not response.get("ok") or not root_ts:
        msg = f"ticket header failed: {response}"
        raise RuntimeError(msg)
    thread.channel = channel
    thread.root_ts = root_ts
    thread.save(update_fields=["channel", "root_ts"])


def _verify_header(request: TicketDmRequest, ticket: Ticket, thread: TicketDmThread, channel: str) -> str:
    try:
        messages = _root_messages(request.backend, channel, thread.root_ts)
    except MessagingReadRefusedError as exc:
        if exc.error_code in _DELETED_ROOT_CODES:
            _retire(thread)
        return f"ticket header unreadable: {exc}"
    except (OSError, RuntimeError, ValueError) as exc:
        return f"ticket header unreadable: {exc}"
    root = next((message for message in messages if str(message.get("ts", "")) == thread.root_ts), None)
    if root is None:
        return "ticket header unverified"
    if root.get("subtype") == "tombstone":
        _retire(thread)
        return "ticket header deleted"
    ThreadBinding.bind(ticket=ticket, channel=channel, thread_ts=thread.root_ts)
    TicketDmPost.objects.get_or_create(
        channel=channel,
        slack_ts=thread.root_ts,
        defaults={"thread": thread, "generation": thread.generation},
    )
    return ""


def _ensure_root(request: TicketDmRequest, ticket: Ticket, channel: str) -> str:
    TicketDmThread.objects.get_or_create(ticket=ticket)
    with transaction.atomic():
        # SQLite ignores select_for_update, so take its write lock before posting.
        TicketDmThread.objects.filter(ticket=ticket).update(generation=F("generation"))
        thread = TicketDmThread.objects.select_for_update().get(ticket=ticket)
        if thread.channel and thread.channel != channel:
            msg = "ticket DM channel changed"
            raise RuntimeError(msg)
        if not thread.root_ts:
            _open_header(request, ticket, thread, channel)
        failure = _verify_header(request, ticket, thread, channel)
        root_ts = thread.root_ts
    if failure:
        raise RuntimeError(failure)
    return root_ts


def deliver_ticket_dm(request: TicketDmRequest) -> tuple[str, str, str]:
    ticket = Ticket.objects.filter(pk=request.ticket_id).first()
    if ticket is None or (ticket.issue_url and not is_forge_url(ticket.issue_url)):
        return request.deliver(
            request.backend, user_id=request.user_id, text=request.text, blocks=request.blocks, thread_ts=""
        )
    if not ticket.issue_url and not (ticket.extra.get("issue_title") or ticket.short_description):
        return request.deliver(
            request.backend, user_id=request.user_id, text=request.text, blocks=request.blocks, thread_ts=""
        )
    root_ready = False

    def root_for_channel(channel: str) -> str:
        nonlocal root_ready
        root = _ensure_root(request, ticket, channel)
        root_ready = True
        return root

    channel, posted_ts, failure = request.deliver(
        request.backend,
        user_id=request.user_id,
        text=request.text,
        blocks=request.blocks,
        thread_ts=root_for_channel,
    )
    thread = TicketDmThread.objects.filter(ticket=ticket).first()
    if failure:
        if (
            root_ready
            and thread is not None
            and thread.root_ts
            and _root_is_deleted(request.backend, thread.channel, thread.root_ts)
        ):
            _retire(thread)
        return channel, posted_ts, failure
    if thread is not None:
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
