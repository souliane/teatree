"""Pre-dispatch forge read-back for the issue-implementer (fleet-safety Stage 1).

A NET, not a mutex. Two teatree instances (a laptop and a headless box) against
one GitHub repo keep per-instance SQLite, so instance B's
:class:`~teatree.core.models.implemented_issue_marker.ImplementedIssueMarker`
claim is invisible to instance A's DB — the cross-instance double-claim site.
Before claiming an issue for auto-implementation, this queries the forge for
live evidence the work already exists — an open PR whose head branch is the
deterministic ``<ticket_number>-*`` worktree branch (:func:`build_branch_name`),
or an open PR that references the issue — and lets the scanner skip the claim
when found. It closes most of the double-claim window, but it cannot close it:
two instances can still both read "clean" in the same tick before either opens a
PR. The durable fleet mutex is Stage 2 (GitHub claim refs); this is the
idempotent safety net beneath it.

The read-back reuses the forge call ``MyPrsScanner`` already makes
(``host.list_my_prs``) and the ``<slug>#N`` close-keyword parse the manual-PR
reconciler uses — it is not a new forge client. Matches are scoped to the
issue's own repo so ``#5`` in one repo never binds a PR from another.
"""

import logging
import re
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlparse

from teatree.core.backend_protocols import CodeHostBackend
from teatree.loop.pr_ticket_index import _parse_closes_ticket
from teatree.types import RawAPIDict
from teatree.utils.url_slug import pr_ref_from_url, slug_from_issue_or_pr_url

logger = logging.getLogger(__name__)

_TRAILING_NUMBER_RE = re.compile(r"(\d+)$")


def issue_number(issue_url: str) -> str:
    """The value the deterministic ``<ticket_number>-*`` worktree branch is built from.

    Mirrors ``Ticket.ticket_number`` — the trailing digits of *issue_url* — so
    the read-back derives the branch prefix from the same value the ticket
    intake will. ``""`` when the URL has no usable trailing number, in which
    case the read-back cannot check by branch and falls through to claim.
    """
    match = _TRAILING_NUMBER_RE.search(issue_url)
    if match and match.group(1) != "0":
        return match.group(1)
    return ""


@dataclass(frozen=True, slots=True)
class ReadbackHit:
    """Live forge evidence that an issue's work already exists.

    ``reason`` names which signal matched (``open_pr_head_branch`` /
    ``open_pr_body_ref`` / ``open_pr_closes_ref``); ``evidence_url`` is the
    offending PR so the skip is auditable.
    """

    reason: str
    evidence_url: str


def _pr_head_branch(raw: RawAPIDict) -> str:
    head = raw.get("head")
    if isinstance(head, dict):
        ref = cast("RawAPIDict", head).get("ref")
        if isinstance(ref, str):
            return ref
    for key in ("source_branch", "head_ref"):
        value = raw.get(key)
        if isinstance(value, str):
            return value
    return ""


def _pr_url(raw: RawAPIDict) -> str:
    for key in ("html_url", "web_url", "url"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _pr_body(raw: RawAPIDict) -> str:
    for key in ("body", "description"):
        value = raw.get(key)
        if isinstance(value, str):
            return value
    return ""


def _same_repo(pr_url: str, issue_slug: str) -> bool:
    ref = pr_ref_from_url(pr_url)
    return ref is not None and ref.slug == issue_slug


def existing_work_for_issue(
    *,
    issue_url: str,
    ticket_number: str,
    open_prs: list[RawAPIDict],
) -> ReadbackHit | None:
    """Scan the forge's open PRs for proof the issue is already being worked.

    Checks *open_prs* (the user's open PRs the loop already fetches) for the
    first of three signals, all scoped to the issue's own repo slug: a head
    branch equal to ``<ticket_number>`` or prefixed ``<ticket_number>-`` (the
    deterministic worktree branch), the issue URL cited in the PR body, or a
    ``Closes #<ticket_number>`` keyword in the body. ``None`` means clean.
    """
    if not ticket_number:
        return None
    issue_slug = slug_from_issue_or_pr_url(urlparse(issue_url).path)
    branch_prefix = f"{ticket_number}-"
    for raw in open_prs:
        pr_url = _pr_url(raw)
        if issue_slug and not _same_repo(pr_url, issue_slug):
            continue
        head = _pr_head_branch(raw)
        if head == ticket_number or head.startswith(branch_prefix):
            return ReadbackHit("open_pr_head_branch", pr_url)
        body = _pr_body(raw)
        if issue_url and issue_url in body:
            return ReadbackHit("open_pr_body_ref", pr_url)
        if _parse_closes_ticket(body) == ticket_number:
            return ReadbackHit("open_pr_closes_ref", pr_url)
    return None


def fetch_open_prs(host: CodeHostBackend, *, authors: tuple[str, ...]) -> list[RawAPIDict]:
    """Union each author's open PRs on the forge, deduped by URL — best-effort.

    The read-back is a net: a forge hiccup must never block the claim path, so
    a failing ``list_my_prs`` degrades to the PRs gathered so far (``[]`` in the
    worst case) and the caller falls through to claim.
    """
    seen: set[str] = set()
    prs: list[RawAPIDict] = []
    for author in authors:
        try:
            fetched = host.list_my_prs(author=author)
        except Exception:
            logger.debug("forge read-back list_my_prs failed for author %s — skipping", author, exc_info=True)
            continue
        for raw in fetched:
            url = _pr_url(raw)
            if url and url in seen:
                continue
            if url:
                seen.add(url)
            prs.append(raw)
    return prs
