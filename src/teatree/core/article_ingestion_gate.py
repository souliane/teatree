"""Per-article approval gate for scanner-ingested third-party prose (#1391).

This module is the **single helper** the ``scanning-news`` skill (and any
sibling third-party-prose scanner) calls instead of ``gh issue create``.
The flow:

1. Scanner triages candidate articles into a list of
    :class:`ArticleCandidate` dicts.
2. Caller invokes :func:`enqueue_candidates_and_notify` — this writes
    one :class:`PendingArticleSuggestion` per new URL (idempotent on
    ``url_hash``) and DMs the user the batch via
    :func:`teatree.core.notify.notify_user`.
3. The user reviews the batch and calls
    ``t3 manage news approve <id>`` / ``t3 manage news reject <id>``
    to act on individual suggestions.
4. ``approve`` is the only path that calls ``gh issue create`` on
    ``souliane/teatree`` with the ``from-news-scan`` label.

The gate is **config-overridable**: when
``[teatree] ask_before_creating_news_tickets = false`` resolves to
``False`` the scanner falls back to direct ticket creation (the legacy
behaviour). The default is ``True`` (gate on).
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.core.models import PendingArticleSuggestion
from teatree.utils.run import CommandFailedError, run_checked

if TYPE_CHECKING:
    from teatree.backends.protocols import MessagingBackend

logger = logging.getLogger(__name__)

#: Label every ask-gate-approved issue carries, matching the legacy
#: skill behaviour so dedup queries keep working.
APPROVED_ISSUE_LABEL = "from-news-scan"

#: Repo the scanning-news skill files to. Hardcoded because the skill
#: is teatree-specific (it mines TLDR/Rundown for teatree improvements).
APPROVED_ISSUE_REPO = "souliane/teatree"


@dataclass(frozen=True, slots=True)
class ArticleCandidate:
    """One triaged article the scanner wants the user to consider.

    ``url`` is the canonical link to the article. ``title`` is the
    human-readable headline. ``summary`` is the short
    "why-this-looks-interesting" blurb the scanner produced. ``source``
    names the producing newsletter (``"tldr-ai"``, ``"rundown-ai"``,
    etc.) and gates per-source rate-limiting if added later.
    """

    url: str
    title: str
    summary: str
    source: str = ""


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    """Outcome of :func:`enqueue_candidates_and_notify`."""

    new_suggestion_ids: list[int]
    skipped_duplicate_urls: list[str]
    dm_sent: bool


def enqueue_candidates_and_notify(
    candidates: list[ArticleCandidate],
    *,
    backend: "MessagingBackend | None" = None,
    user_id: str | None = None,
    idempotency_prefix: str = "news-scan-batch",
) -> EnqueueResult:
    """Record candidates and DM the user the pending batch.

    Each new URL becomes one :class:`PendingArticleSuggestion` row;
    URLs whose ``url_hash`` is already on file are reported as
    ``skipped_duplicate_urls`` (no second DM, no second row). The DM
    listing names every newly-recorded suggestion by id + title with a
    pointer to ``t3 manage news approve <id>`` / ``... reject <id>``.
    """
    new_ids: list[int] = []
    skipped: list[str] = []
    for candidate in candidates:
        row = PendingArticleSuggestion.record_if_new(
            url=candidate.url,
            summary=candidate.summary,
            title=candidate.title,
            source=candidate.source,
        )
        if row is None:
            skipped.append(candidate.url)
        else:
            new_ids.append(row.pk)

    if not new_ids:
        return EnqueueResult(new_suggestion_ids=[], skipped_duplicate_urls=skipped, dm_sent=False)

    dm_text = _format_batch_dm(new_ids)
    sent = _notify_user(
        text=dm_text,
        backend=backend,
        user_id=user_id,
        idempotency_key=f"{idempotency_prefix}:{'-'.join(str(i) for i in new_ids)}",
    )
    if sent:
        PendingArticleSuggestion.mark_batch_presented(new_ids)
    return EnqueueResult(
        new_suggestion_ids=new_ids,
        skipped_duplicate_urls=skipped,
        dm_sent=sent,
    )


def _format_batch_dm(suggestion_ids: list[int]) -> str:
    rows = PendingArticleSuggestion.objects.filter(pk__in=suggestion_ids).order_by("created_at")
    lines = [f":newspaper: *{len(rows)} candidate article(s) from news-scan*"]
    for row in rows:
        title = row.title or row.url
        lines.extend([f"  #{row.pk}: {title}", f"     {row.url}"])
        if row.summary:
            lines.append(f"     {row.summary}")
    lines.extend(
        [
            "",
            "Approve: `t3 manage news approve <id>` — Reject: `t3 manage news reject <id>`",
        ],
    )
    return "\n".join(lines)


def _notify_user(
    *,
    text: str,
    backend: "MessagingBackend | None",
    user_id: str | None,
    idempotency_key: str,
) -> bool:
    """Send the batch DM via the canonical bot→user helper.

    Imported lazily so this module stays importable in tests that don't
    pre-build the Django + messaging machinery.
    """
    from teatree.core.notify import NotifyKind, notify_user  # noqa: PLC0415

    return notify_user(
        text,
        kind=NotifyKind.INFO,
        idempotency_key=idempotency_key,
        backend=backend,
        user_id=user_id,
    )


def approve_and_create_ticket(
    suggestion_id: int,
    *,
    decider_id: str = "",
    repo: str = APPROVED_ISSUE_REPO,
    label: str = APPROVED_ISSUE_LABEL,
    extra_body_lines: list[str] | None = None,
) -> PendingArticleSuggestion | None:
    """Approve a suggestion and create the corresponding GitHub issue.

    Calls ``gh issue create`` once and stamps the resulting URL on the
    :class:`PendingArticleSuggestion` row inside the same atomic step.
    Returns the consumed row on success, ``None`` when the suggestion
    is missing or already decided. Raises
    :class:`teatree.utils.run.CommandFailedError` when ``gh`` exits
    non-zero — the row stays pending so the caller can retry after
    fixing the gh state.
    """
    row = PendingArticleSuggestion.objects.filter(pk=suggestion_id).first()
    if row is None or not row.is_pending:
        return None
    title = (row.title or row.url)[:200]
    body_lines = [
        f"Source: {row.url}",
        "",
        row.summary,
    ]
    if extra_body_lines:
        body_lines.extend(["", *extra_body_lines])
    body = "\n".join(body_lines)
    ticket_url = _gh_issue_create(repo=repo, title=title, body=body, label=label)
    return PendingArticleSuggestion.approve(
        suggestion_id,
        decider_id=decider_id,
        ticket_url=ticket_url,
    )


def _gh_issue_create(*, repo: str, title: str, body: str, label: str) -> str:
    """Thin shell around ``gh issue create`` returning the issue URL.

    Separated so tests monkeypatch this single chokepoint instead of
    the surrounding business logic.
    """
    try:
        result = run_checked(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                repo,
                "--title",
                title,
                "--body",
                body,
                "--label",
                label,
            ],
        )
    except CommandFailedError:
        logger.exception("gh issue create failed for repo=%s title=%s", repo, title[:60])
        raise
    stdout = (result.stdout or "").strip()
    if not stdout:
        return ""
    return stdout.splitlines()[-1].strip()


def ask_gate_enabled() -> bool:
    """Resolve the ``ask_before_creating_news_tickets`` setting (default True).

    The gate is on by default; users who explicitly opt back into legacy
    behaviour set ``[teatree] ask_before_creating_news_tickets = false``
    in ``~/.teatree.toml``. Per-overlay overrides are supported via the
    standard ``OVERLAY_OVERRIDABLE_SETTINGS`` chain.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415

    return bool(get_effective_settings().ask_before_creating_news_tickets)
