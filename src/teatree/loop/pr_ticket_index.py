"""Build a ``mr_url → parent_ticket_number`` index for statusline grouping.

Two sources, cheapest first:

1.  ``PullRequest.ticket`` FK — authoritative when the row exists. Persisted
    when the user runs ``ship``, so any MR that went through the standard
    pipeline appears here.
2.  ``Closes/Fixes #N`` footer parsed from the MR description carried on the
    ``ScanSignal`` payload. Free fallback for PRs whose ``PullRequest`` row
    never got created (manual MRs, MRs opened in a different overlay) so the
    statusline still buckets them under the parent ticket they reference.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Any

from teatree.loop.dispatch import DispatchAction

type Payload = Mapping[str, Any]

# Matches ``Closes #123`` / ``Fixes: #456`` / ``Resolves #789`` (and the
# plural/punctuation variants). Anchored at a word boundary so it doesn't
# match ``preCloses#`` or similar. Same vocabulary as
# ``teatree.core.runners.ship.sanitize_close_keywords`` to stay consistent.
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


def build_ticket_index(actions: Iterable[DispatchAction]) -> dict[str, str]:
    """Map every MR URL in *actions* to its parent ticket number.

    Order of resolution per URL:

    1.  ``PullRequest.ticket`` FK lookup (authoritative).
    2.  ``Closes/Fixes #N`` footer parsed from the MR description carried on
        the signal payload.

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
    return index
