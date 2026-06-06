"""Build a ``mr_url → parent_ticket_number`` index for statusline grouping.

Three sources, cheapest first:

1.  ``PullRequest.ticket`` FK — authoritative when the row exists. Persisted
    when the user runs ``ship``, so any MR that went through the standard
    pipeline appears here.
2.  ``Closes/Fixes #N`` footer parsed from the MR description carried on the
    ``ScanSignal`` payload. Free fallback for PRs whose ``PullRequest`` row
    never got created (manual MRs, MRs opened in a different overlay) so the
    statusline still buckets them under the parent ticket they reference.
3.  ``Ticket.extra["prs"]["<url>"]`` — last-priority fallback (#1113 Defect 3)
    for a bare manually-opened MR that has neither an FK row nor a close-keyword
    footer. The ship pipeline records every MR under its ticket's
    ``extra["prs"]`` dict (see ``ReviewRequestPost`` docstring and
    ``gitlab_sync_prs``); the renderer reads the same map so the row buckets
    under its ticket instead of orphaning detached at the tail.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Any

from teatree.loop.dispatch import DispatchAction

type Payload = Mapping[str, Any]

# Matches ``Closes #123`` / ``Fixes: #456`` / ``Resolves #789`` (and the
# plural/past-tense variants the platforms recognise — ``closed``, ``fixed``,
# ``resolved``). Broader than ``sanitize_close_keywords`` in ship.py because
# the parser must accept anything GitHub/GitLab auto-link, not just the
# subset we emit. Anchored at a word boundary so ``preCloses#`` doesn't match.
_CLOSE_KEYWORD_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[\s:]*#(\d+)",
    re.IGNORECASE,
)


def _description_from_payload(payload: Payload) -> str:
    raw = payload.get("raw")
    if isinstance(raw, Mapping):
        for key in ("description", "body"):
            value = raw.get(key)
            if isinstance(value, str):
                return value
    return ""


def _parse_closes_ticket(description: str) -> str:
    """Return the first ``#N`` mentioned after a Closes/Fixes/Resolves keyword.

    Returns an empty string if no close-keyword is found.
    """
    match = _CLOSE_KEYWORD_RE.search(description)
    return match.group(1) if match else ""


def _mr_url_payloads(actions: Iterable[DispatchAction]) -> dict[str, Payload]:
    """Collect ``url → payload`` for MR-bearing statusline actions."""
    out: dict[str, Payload] = {}
    for action in actions:
        if action.kind != "statusline":
            continue
        if action.zone not in {"action_needed", "in_flight"}:
            continue
        payload = action.payload if isinstance(action.payload, Mapping) else {}
        url = payload.get("url")
        if isinstance(url, str) and url:
            out[url] = payload
    return out


def _lookup_pr_tickets(urls: Iterable[str]) -> dict[str, str]:
    """Return ``url → ticket_number`` for MRs persisted as ``PullRequest`` rows.

    Falls back to an empty dict when Django isn't ready (e.g. unit tests that
    didn't load the apps) — the parser path still works.
    """
    url_list = [u for u in urls if u]
    if not url_list:
        return {}
    try:
        from django.apps import apps  # noqa: PLC0415

        pr_model = apps.get_model("core", "PullRequest")
    except Exception:  # noqa: BLE001
        return {}
    result: dict[str, str] = {}
    try:
        rows = (
            pr_model.objects.filter(url__in=url_list)
            .select_related("ticket")
            .only("url", "ticket__issue_url", "ticket__id")
        )
        for row in rows:
            ticket = row.ticket
            if ticket is None:
                continue
            number = ticket.ticket_number
            if number:
                result[row.url] = number
    except Exception:  # noqa: BLE001
        return {}
    return result


def _lookup_ticket_extra_prs(urls: Iterable[str]) -> dict[str, str]:
    """Return ``url → ticket_number`` via ``Ticket.extra["prs"][<url>]`` (#1113).

    Last-priority fallback for an MR with no ``PullRequest`` FK row and no
    ``Closes #N`` footer. The ship pipeline records every MR under its
    ticket's ``extra["prs"]`` dict (see ``backends/gitlab/sync_prs``), so
    walking that map closes the index gap for bare manually-opened MRs.
    Falls back to an empty dict when Django isn't ready or a query fails so
    the FK + footer paths still resolve normally.
    """
    url_set = {u for u in urls if u}
    if not url_set:
        return {}
    try:
        from django.apps import apps  # noqa: PLC0415

        ticket_model = apps.get_model("core", "Ticket")
    except Exception:  # noqa: BLE001
        return {}
    result: dict[str, str] = {}
    try:
        rows = ticket_model.objects.exclude(extra={}).only("issue_url", "extra", "id")
        for ticket in rows:
            extra = ticket.extra if isinstance(ticket.extra, dict) else {}
            prs = extra.get("prs") if isinstance(extra, dict) else None
            if not isinstance(prs, dict):
                continue
            number = ticket.ticket_number
            if not number:
                continue
            for pr_url in prs:
                if isinstance(pr_url, str) and pr_url in url_set and pr_url not in result:
                    result[pr_url] = number
    except Exception:  # noqa: BLE001
        return {}
    return result


def build_ticket_index(actions: Iterable[DispatchAction]) -> dict[str, str]:
    """Map every MR URL in *actions* to its parent ticket number.

    Order of resolution per URL:

    1.  ``PullRequest.ticket`` FK lookup (authoritative).
    2.  ``Closes/Fixes #N`` footer parsed from the MR description carried on
        the signal payload.
    3.  ``Ticket.extra["prs"]["<url>"]`` walk — bare manually-opened MRs
        recorded by the ship pipeline (#1113 Defect 3).

    Missing entries simply aren't in the result — the caller treats them as
    orphans and renders them at the tail of the overlay's PR group.
    """
    payloads = _mr_url_payloads(actions)
    if not payloads:
        return {}
    index = _lookup_pr_tickets(payloads.keys())
    for url, payload in payloads.items():
        if url in index:
            continue
        ticket_number = _parse_closes_ticket(_description_from_payload(payload))
        if ticket_number:
            index[url] = ticket_number
    unresolved = [url for url in payloads if url not in index]
    if unresolved:
        extra_map = _lookup_ticket_extra_prs(unresolved)
        for url, number in extra_map.items():
            if url not in index:
                index[url] = number
    return index
