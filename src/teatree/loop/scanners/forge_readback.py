"""Pre-dispatch forge read-back for the issue-implementer (fleet-safety Stage 1).

A NET, not a mutex. Two teatree instances (a laptop and a headless box) against
one GitHub repo keep per-instance SQLite, so instance B's
:class:`~teatree.core.models.implemented_issue_marker.ImplementedIssueMarker`
claim is invisible to instance A's DB — the cross-instance double-claim site.
Before claiming an issue for auto-implementation, this queries the forge for
live evidence the work already exists — an open OR merged PR whose head branch
cites the ticket number (the deterministic ``<ticket_number>-*`` worktree branch
or a hand-named branch that embeds it), or an open/merged PR that references the
issue by URL or ``#<ticket_number>`` — and lets the scanner skip the claim when
found. Merged PRs count so a fully-implemented issue is not re-claimed once its
PRs land. It closes most of the double-claim window, but it cannot close it:
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
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlparse

from teatree.core.backend_protocols import CodeHostBackend
from teatree.types import RawAPIDict
from teatree.utils.close_keywords import parse_closes_ticket
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

    ``reason`` names which signal matched — a ``<state>_pr_<signal>`` pair where
    ``state`` is ``open`` or ``merged`` and ``signal`` is ``head_branch`` /
    ``body_ref`` / ``closes_ref`` / ``cited_ref``; ``evidence_url`` is the
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


def _pr_title(raw: RawAPIDict) -> str:
    value = raw.get("title")
    return value if isinstance(value, str) else ""


def _same_repo(pr_url: str, issue_slug: str) -> bool:
    ref = pr_ref_from_url(pr_url)
    return ref is not None and ref.slug == issue_slug


def _pr_signal(raw: RawAPIDict, *, issue_url: str, ticket_number: str, issue_slug: str) -> str | None:
    """Which signal (if any) proves *raw* is work for the issue, else ``None``.

    Scoped to the issue's own repo slug so ``#5`` / a ``5-*`` branch in a
    FOREIGN repo never binds. The head-branch match is a whole-word test on the
    ticket number, so both the deterministic ``<ticket_number>`` / ``<ticket_number>-*``
    worktree branch AND a hand-named branch that embeds the number
    (``impl-<ticket_number>-presets``, ``fix-<ticket_number>``) count. The
    ``#<ticket_number>`` citation is likewise a whole-word test so ``#420`` never
    binds issue ``42``. Returns the bare signal (``head_branch`` / ``body_ref`` /
    ``closes_ref`` / ``cited_ref``); the caller prefixes it with the PR state.
    """
    pr_url = _pr_url(raw)
    if not _same_repo(pr_url, issue_slug):
        return None
    if re.search(rf"\b{re.escape(ticket_number)}\b", _pr_head_branch(raw)):
        return "head_branch"
    body = _pr_body(raw)
    if issue_url and issue_url in body:
        return "body_ref"
    if parse_closes_ticket(body) == ticket_number:
        return "closes_ref"
    cite = re.compile(rf"#{re.escape(ticket_number)}\b")
    if cite.search(_pr_title(raw)) or cite.search(body):
        return "cited_ref"
    return None


def existing_work_for_issue(
    *,
    issue_url: str,
    ticket_number: str,
    open_prs: list[RawAPIDict],
    merged_prs: list[RawAPIDict] | None = None,
) -> ReadbackHit | None:
    """Scan the forge's open and merged PRs for proof the issue is already worked.

    Checks *open_prs* then *merged_prs* (the user's open and merged PRs the loop
    fetches) for the first of four signals, all scoped to the issue's own repo
    slug: a head branch that cites ``<ticket_number>`` as a whole word (the
    deterministic ``<ticket_number>``/``<ticket_number>-*`` worktree branch OR a
    hand-named branch that embeds it, e.g. ``impl-<ticket_number>-presets``), the
    issue URL cited in the PR body, a ``Closes #<ticket_number>`` keyword, or a
    bare ``#<ticket_number>`` citation in the PR title or body. Merged PRs count
    so a fully-implemented issue is not re-claimed once its PRs land. ``None``
    means clean.

    Fails OPEN when the issue's own repo slug is unparsable (a synthetic or
    non-standard issue URL): without a slug to scope by, a ``<ticket_number>-*``
    branch or a ``Closes #<ticket_number>`` in a FOREIGN repo would match and
    strand the issue on a spurious skip. Consistent with the module's fail-open
    design (a forge error also degrades to "claim anyway"), the read-back yields
    no match and the caller claims.
    """
    if not ticket_number:
        return None
    issue_slug = slug_from_issue_or_pr_url(urlparse(issue_url).path)
    if not issue_slug:
        return None
    for state, prs in (("open", open_prs), ("merged", merged_prs or [])):
        for raw in prs:
            signal = _pr_signal(raw, issue_url=issue_url, ticket_number=ticket_number, issue_slug=issue_slug)
            if signal is not None:
                return ReadbackHit(f"{state}_pr_{signal}", _pr_url(raw))
    return None


def fetch_open_prs(host: CodeHostBackend, *, authors: tuple[str, ...]) -> list[RawAPIDict]:
    """Union each author's OPEN PRs on the forge, deduped by URL — best-effort.

    The read-back is a net: a forge hiccup must never block the claim path, so
    a failing ``list_my_prs`` degrades to the PRs gathered so far (``[]`` in the
    worst case) and the caller falls through to claim.
    """
    return _union_prs(host.list_my_prs, authors=authors, kind="list_my_prs")


def fetch_merged_prs(host: CodeHostBackend, *, authors: tuple[str, ...]) -> list[RawAPIDict]:
    """Union each author's MERGED PRs on the forge, deduped by URL — best-effort.

    The merged-PR companion to :func:`fetch_open_prs`: an issue whose
    implementing PRs have already landed must not be re-claimed. Same fail-open
    contract — a failing ``list_my_merged_prs`` degrades to the PRs gathered so
    far and the caller falls through to claim.
    """
    return _union_prs(host.list_my_merged_prs, authors=authors, kind="list_my_merged_prs")


def _union_prs(
    lister: Callable[..., list[RawAPIDict]],
    *,
    authors: tuple[str, ...],
    kind: str,
) -> list[RawAPIDict]:
    seen: set[str] = set()
    prs: list[RawAPIDict] = []
    for author in authors:
        try:
            fetched = lister(author=author)
        except Exception:
            logger.debug("forge read-back %s failed for author %s — skipping", kind, author, exc_info=True)
            continue
        for raw in fetched:
            url = _pr_url(raw)
            if url and url in seen:
                continue
            if url:
                seen.add(url)
            prs.append(raw)
    return prs
