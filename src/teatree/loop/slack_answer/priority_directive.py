"""Owner Slack directives for the existing ticket expedite flag."""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import escape
from typing import TYPE_CHECKING, Literal

from django.db import transaction

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models import DeferredQuestion, PendingChatInjection, ThreadBinding, Ticket
from teatree.loop.question_binding import configured_owner_id
from teatree.loop.slack_answer.session_routing import _mark_unit_loop_replied, _unmark_unit_loop_replied
from teatree.loop.slack_answer.thread_readback import ExpectedReply, bot_reply_present_in_thread
from teatree.slack_mrkdwn import wrap_slack_message
from teatree.utils.url_slug import is_synthetic_loop_umbrella_url, repo_namespaced_key

if TYPE_CHECKING:
    from teatree.loop.slack_answer.cycle import SlackAnswerReport, _Unit

_LINK = re.compile(r"<[^>]+>")
_URL = re.compile(r"https?://[^\s<>|)]+")
_REPO_REF = re.compile(r"(?<![\w/])(?:[\w.-]+/)+[\w.-]+#\d+\b")
_BARE_REF = re.compile(r"(?<![\w/])#(\d+)\b")
_CUE = r"(?:urgent(?:ly)?|hot-?fix|top priority|highest priority|release blocker|blocker|a\.?s\.?a\.?p\.?)"
_ON = re.compile(rf"\b{_CUE}(?=$|\W)", re.IGNORECASE)
_OFF = re.compile(
    rf"\b(?:not|never|non[- ]|no longer(?: an?| the)?|no|isn['\u2019]t)\s*-?\s*(?:a\s+)?{_CUE}(?=$|\W)",
    re.IGNORECASE,
)
_SOFT_OFF = re.compile(r"\b(?:later|no rush|can wait|low priority|deprioriti[sz]e)\b", re.IGNORECASE)
_NEGATOR = re.compile(
    r"\b(?:not|never|no|nothing|(?:don|doesn|isn|aren|can|w(?:as|o)n)['\u2019]t)\b",
    re.IGNORECASE,
)
_QUESTION = re.compile(
    r"^\s*(?:what|how|why|which|who|when|where)\b|^\s*do you (?:think|believe|know)\b"
    r"|^\s*(?:is|are|was|were)\s+(?:it|this|that|these|those|#\d+|[\w.-]+/[\w.-]+#\d+)\b",
    re.IGNORECASE,
)
_BLOCKER_NOUN = re.compile(r"\bblocker\s+(?:is|are|was|were)\s+\w", re.IGNORECASE)
_WHOLE_FILLER = frozenset(
    {
        "this",
        "is",
        "it",
        "please",
        "the",
        "ticket",
        "tickets",
        "mark",
        "make",
        "set",
        "as",
        "now",
        "these",
        "and",
        "are",
        "a",
        "an",
        "them",
    }
)
_QUESTION_PREFIX = re.compile(r"^\s*#(\d+)(?:\s|$)")
type PriorityOutcome = Literal["none", "mixed_on", "mixed_off", "confirmed", "retry"]


@dataclass(frozen=True, slots=True)
class PriorityDirective:
    enabled: bool
    only: bool


def parse_priority_directive(text: str) -> PriorityDirective | None:
    """Return a directive only when its polarity is unambiguous."""
    body = _URL.sub(" ", _LINK.sub(" ", text))
    sentences = re.split(r"(?<=[.!?])\s+|\n+", body)
    if "?" in body or any(_QUESTION.search(sentence) for sentence in sentences) or _BLOCKER_NOUN.search(body):
        return None
    off_spans = list(_OFF.finditer(body))
    without_off = _OFF.sub(" ", body)
    on_spans = list(_ON.finditer(without_off))
    soft_spans = list(_SOFT_OFF.finditer(without_off))
    mixed_polarity = bool(on_spans and (off_spans or soft_spans))
    if _NEGATOR.search(_SOFT_OFF.sub(" ", without_off)) or mixed_polarity:
        return None
    if not off_spans and not on_spans and not soft_spans:
        return None
    remainder = _OFF.sub(" ", body)
    remainder = _ON.sub(" ", remainder)
    remainder = _SOFT_OFF.sub(" ", remainder)
    remainder = _REPO_REF.sub(" ", _BARE_REF.sub(" ", remainder))
    words = re.findall(r"[a-z]+|\d+", remainder.casefold())
    only = all(word in _WHOLE_FILLER for word in words)
    if soft_spans and not only and not off_spans:
        return None
    return PriorityDirective(enabled=bool(on_spans), only=only)


def _reference_tokens(text: str) -> tuple[list[str], bool]:
    urls = [match.group().rstrip(".,;!?") for match in _URL.finditer(text)]
    without_links = _LINK.sub(" ", text)
    without_urls = _URL.sub(" ", without_links)
    repos = [match.group() for match in _REPO_REF.finditer(without_urls)]
    without_repos = _REPO_REF.sub(" ", without_urls)
    bare = [match.group(1) for match in _BARE_REF.finditer(without_repos)]
    return [*urls, *repos, *bare], bool(bare)


def _eligible(ticket: Ticket) -> bool:
    return bool(
        ticket.repo_namespaced_key
        and not is_synthetic_loop_umbrella_url(ticket.issue_url)
        and ticket.role == Ticket.Role.AUTHOR
        and not ticket.is_settled
        and ticket.state not in Ticket.finished_states()
    )


def _resolve_tokens(tokens: list[str]) -> list[Ticket]:
    tickets: dict[int, Ticket] = {}
    for token in tokens:
        if token.isdigit():
            candidates = [candidate for candidate in Ticket.objects.filter(issue_number=token) if _eligible(candidate)]
        else:
            key = repo_namespaced_key(token) if token.startswith(("http://", "https://")) else token
            candidates = (
                [candidate for candidate in Ticket.objects.filter(repo_namespaced_key=key) if _eligible(candidate)]
                if key
                else []
            )
        if len(candidates) != 1:
            return []
        tickets[candidates[0].pk] = candidates[0]
    return list(tickets.values())


def _thread_ticket(
    backend: MessagingBackend, row: PendingChatInjection, binding: ThreadBinding | None
) -> Ticket | None:
    if not row.thread_ts:
        return None
    question = (
        DeferredQuestion.objects.filter(slack_channel=row.channel, slack_ts=row.thread_ts, parked_task__isnull=False)
        .select_related("parked_task__ticket")
        .order_by("-created_at", "-pk")
        .first()
    )
    if question is not None and question.parked_task is not None:
        return question.parked_task.ticket
    if binding is not None:
        return binding.ticket
    root = backend.fetch_message(channel=row.channel, ts=row.thread_ts)
    root_text = root.get("text")
    if not isinstance(root_text, str):
        return None
    tokens, _bare = _reference_tokens(root_text)
    tickets = _resolve_tokens(tokens)
    return tickets[0] if len(tickets) == 1 else None


def select_tickets(
    backend: MessagingBackend,
    row: PendingChatInjection,
    text: str,
    directive: PriorityDirective,
    binding: ThreadBinding | None,
) -> list[Ticket]:
    tokens, has_bare = _reference_tokens(text)
    if tokens:
        if has_bare and not directive.only:
            return []
        tickets = _resolve_tokens(tokens)
        return tickets if directive.only or len(tickets) == 1 else []
    ticket = _thread_ticket(backend, row, binding)
    return [ticket] if ticket is not None and _eligible(ticket) else []


def pending_question_prefix(row: PendingChatInjection, text: str) -> bool:
    match = _QUESTION_PREFIX.match(text)
    return bool(
        match
        and DeferredQuestion.objects.filter(
            pk=int(match.group(1)), slack_channel=row.channel, answered_at__isnull=True, dismissed_at__isnull=True
        ).exists()
    )


def apply_priority(ticket: Ticket, row: PendingChatInjection, *, enabled: bool) -> tuple[Ticket, bool]:
    """Apply the newest owner instruction in one conversation and re-read it."""
    key = f"{row.channel}:{row.thread_ts}:{row.user_id}"
    with transaction.atomic():
        current = Ticket.objects.select_for_update().get(pk=ticket.pk)
        extra = dict(current.extra or {})
        receipts = dict(extra.get("slack_priority_receipts") or {})
        prior = receipts.get(key)
        try:
            newer = prior is None or Decimal(row.slack_ts) >= Decimal(prior)
        except (InvalidOperation, TypeError):
            newer = False
        if newer:
            current.expedited = enabled
            receipts[key] = row.slack_ts
            extra["slack_priority_receipts"] = receipts
            current.extra = extra
            current.save(update_fields=["expedited", "extra"])
    current.refresh_from_db(fields=["expedited", "extra"])
    return current, not newer


def priority_reply(tickets: list[Ticket], *, enabled: bool | None = None, superseded: bool = False) -> str:
    lines = []
    for ticket in tickets:
        title = escape(ticket.short_description or "Ticket", quote=False).replace("|", "❘")
        reference = f"<{ticket.issue_url}|#{ticket.issue_number} {title}>"
        expedited = ticket.expedited if enabled is None else enabled
        prefix = "Earlier priority instruction superseded; " if superseded else ""
        if expedited:
            lines.append(
                f"{reference}: {prefix}expedited; pending tasks now rank first at admission after reviews. "
                "No merge gate is relaxed."
            )
        else:
            lines.append(f"{reference}: {prefix}expedite cleared; tasks rank normally again.")
    return "\n".join(lines)


def confirm_priority_reply(
    backend: MessagingBackend, row: PendingChatInjection, floor: str, reply: str, *, previous_reply: str = ""
) -> bool:
    root = row.thread_ts or row.slack_ts
    expected = ExpectedReply(None, wrap_slack_message(reply))
    if previous_reply and bot_reply_present_in_thread(
        backend,
        channel=row.channel,
        thread_root=root,
        after_ts=floor,
        expected_ts=ExpectedReply(None, wrap_slack_message(previous_reply)),
    ):
        return True
    if bot_reply_present_in_thread(
        backend, channel=row.channel, thread_root=root, after_ts=floor, expected_ts=expected
    ):
        return True
    post = backend.post_reply(channel=row.channel, ts=row.slack_ts, text=expected.text)
    posted_ts = post.get("ts")
    return bool(
        post.get("ok") is True
        and isinstance(posted_ts, str)
        and bot_reply_present_in_thread(
            backend,
            channel=row.channel,
            thread_root=root,
            after_ts=floor,
            expected_ts=ExpectedReply(posted_ts, expected.text),
        )
    )


def handle_priority_directive(
    backend: MessagingBackend,
    unit: "_Unit",
    report: "SlackAnswerReport",
    binding: ThreadBinding | None,
) -> PriorityOutcome:
    directive = parse_priority_directive(unit.text)
    owner_id = configured_owner_id(backend)
    if directive is None or not owner_id or unit.lead.user_id != owner_id:
        return "none"
    if pending_question_prefix(unit.lead, unit.text):
        return "none"
    tickets = select_tickets(backend, unit.lead, unit.text, directive, binding)
    if not tickets:
        return "none"
    if not directive.only:
        for ticket in tickets:
            apply_priority(ticket, unit.lead, enabled=directive.enabled)
        return "mixed_on" if directive.enabled else "mixed_off"
    return _confirm_pure_priority(backend, unit, report, tickets, enabled=directive.enabled)


def _confirm_pure_priority(
    backend: MessagingBackend,
    unit: "_Unit",
    report: "SlackAnswerReport",
    tickets: list[Ticket],
    *,
    enabled: bool,
) -> PriorityOutcome:
    if not _mark_unit_loop_replied(unit, PendingChatInjection.AnswerKind.PRIORITY):
        return "retry"
    try:
        outcomes = [apply_priority(ticket, unit.lead, enabled=enabled) for ticket in tickets]
        changed = [ticket for ticket, _superseded in outcomes]
        superseded = any(was_superseded for _ticket, was_superseded in outcomes)
        reply = priority_reply(changed, superseded=superseded)
        previous_reply = priority_reply(changed, enabled=enabled) if superseded else ""
        if not confirm_priority_reply(backend, unit.lead, unit.rows[-1].slack_ts, reply, previous_reply=previous_reply):
            _unmark_unit_loop_replied(unit)
            return "retry"
    except Exception:
        _unmark_unit_loop_replied(unit)
        raise
    for row in unit.rows:
        row.observe_confirmed_loop_reply()
    report.answered_simple += 1
    return "confirmed"
