"""Forge-authoritative open-PR count for the per-(repo, ticket) budget (fleet-safety Stage 3).

The local union in :mod:`pr_budget_gate` (``PullRequest`` rows unioned with
``ticket.extra["pr_url_by_branch"]``) only sees PRs THIS fleet instance recorded.
A sibling instance's just-opened PR is invisible until ``MyPrsScanner`` +
``manual_pr_reconcile`` import it on a later tick — a one-tick window in which two
fleet instances can each open a PR for the same ticket, breaking invariant I2 (at
most one open PR per ``(ticket, repo)``). GitHub's one-PR-per-head-branch rule and
deterministic branch names foreclose the identical-branch case, but NOT two
different branches for the same ticket.

This adds the authoritative third source: a live forge read (``host.list_my_prs``,
the same client the PR scanners already use) scoped to the ticket's repo, attributed
to the ticket by the same ``<slug>#<n>`` close-keyword join the tick reconciler uses
(:mod:`teatree.loop.manual_pr_reconcile`). Because ``teatree.core`` (domain) may not
import ``teatree.loop`` (orchestration), the shared attribution primitive lives in
:mod:`teatree.utils.close_keywords`; this module depends only on the injected
``CodeHostBackend`` Protocol, not on any concrete backend.

Fails OPEN: a forge/network/rate-limit error, an unresolvable identity, or a
missing host all degrade to :data:`None` so the caller falls back to the local-only
budget. A forge outage must never block all shipping.
"""

import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING

from teatree.config import get_effective_settings
from teatree.core.models import Ticket
from teatree.utils.close_keywords import parse_closes_ticket
from teatree.utils.run import CommandFailedError
from teatree.utils.url_slug import PrRef, pr_ref_from_url

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend
    from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)

# A forge transport failure — non-zero ``gh``/``glab`` (network, auth,
# rate-limit), a subprocess spawn error, or a malformed JSON payload
# (``json.JSONDecodeError`` is a ``ValueError``). Any of these degrades the
# authoritative check to the local-only budget rather than blocking the ship.
_FORGE_ERRORS = (CommandFailedError, OSError, ValueError)

# The two ship seams (the pre-push fail-fast gate and the create chokepoint)
# fire seconds apart in one flow. Memoising the forge read for this window
# collapses them to a single ``list_my_prs`` call per (ticket, repo).
_CACHE_TTL_SECONDS = 30.0
_forge_cache: dict[tuple[int, str], tuple[float, set[str]]] = {}


def forge_open_pr_urls_for_ticket(
    ticket: Ticket,
    repo_slug: str,
    *,
    host: "CodeHostBackend | None",
) -> set[str] | None:
    """Live open-PR URLs the forge reports for exactly ``(ticket, repo_slug)``.

    The authoritative third source beside the local union in
    :mod:`pr_budget_gate`: a sibling fleet instance's just-opened PR is visible
    here — via ``host.list_my_prs`` — before the local reconciler imports it,
    closing the one-tick race in which two instances each open a PR for one
    ticket. Scoped to *repo_slug* (a PR in another repo is dropped, so no
    cross-repo false block) and attributed to the ticket by the same
    ``<slug>#<n>`` close-keyword join the tick reconciler uses.

    Returns :data:`None` — the fail-OPEN signal — when the forge cannot be
    queried (no *host*, no resolvable identity, or a network/auth/rate-limit
    error), so the caller degrades to the local-only budget.
    """
    if host is None:
        return None
    try:
        authors = _resolve_authors(ticket, host)
        if not authors:
            return None
        return _collect_ticket_pr_urls(ticket, repo_slug, host, authors)
    except _FORGE_ERRORS:
        logger.warning(
            "forge PR-budget backstop skipped for ticket %s in %r — forge query failed; "
            "degrading to the local-only budget",
            ticket.ticket_number or ticket.pk,
            repo_slug,
            exc_info=True,
        )
        return None


def cached_forge_open_pr_urls_for_ticket(
    ticket: Ticket,
    repo_slug: str,
    *,
    host: "CodeHostBackend | None",
) -> set[str] | None:
    """:func:`forge_open_pr_urls_for_ticket`, memoised per ``(ticket, repo)`` for a short window.

    Only a successful (non-``None``) result is cached, so a transient forge
    failure is never sticky — the next seam re-queries. Returns a copy so a
    caller cannot mutate the cached set.
    """
    key = (ticket.pk, repo_slug)
    now = time.monotonic()
    cached = _forge_cache.get(key)
    if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
        return set(cached[1])
    result = forge_open_pr_urls_for_ticket(ticket, repo_slug, host=host)
    if result is not None:
        _forge_cache[key] = (now, set(result))
    return result


def reset_forge_pr_budget_cache() -> None:
    """Clear the per-``(ticket, repo)`` forge memo (tests, overlay reload)."""
    _forge_cache.clear()


def _resolve_authors(ticket: Ticket, host: "CodeHostBackend") -> tuple[str, ...]:
    """The forge identities to union-query, mirroring ``MyPrsScanner``.

    Configured ``user_identity_aliases`` win verbatim (a fleet running under
    several handles); otherwise the single ``host.current_user()``. A sibling
    fleet instance running as the same user is caught either way.
    """
    aliases = tuple(get_effective_settings(ticket.overlay or None).user_identity_aliases)
    if aliases:
        return tuple(dict.fromkeys(aliases))
    user = host.current_user()
    return (user,) if user else ()


def _collect_ticket_pr_urls(
    ticket: Ticket,
    repo_slug: str,
    host: "CodeHostBackend",
    authors: tuple[str, ...],
) -> set[str]:
    """Union open PRs across *authors*, keeping those for exactly ``(ticket, repo_slug)``."""
    urls: set[str] = set()
    seen: set[str] = set()
    for author in authors:
        for raw in host.list_my_prs(author=author):
            url = str(raw.get("web_url") or raw.get("html_url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            ref = pr_ref_from_url(url)
            if ref is None or ref.slug != repo_slug:
                continue
            if _references_ticket(raw, ref, ticket):
                urls.add(url)
    return urls


def _references_ticket(raw: "RawAPIDict", ref: PrRef, ticket: Ticket) -> bool:
    """True iff *raw*'s close-keyword footer resolves to *ticket* in the PR's own repo.

    The ``<slug>#<n>`` repo-namespaced key is the collision-free join the tick
    reconciler uses, so issue #N of one repo never binds a PR from another.
    """
    number = parse_closes_ticket(_pr_body(raw))
    if not number:
        return False
    with suppress(Ticket.DoesNotExist):
        return Ticket.objects.resolve(f"{ref.slug}#{number}").pk == ticket.pk
    return False


def _pr_body(raw: "RawAPIDict") -> str:
    """The PR body across host shapes — GitLab ``description``, GitHub ``body``."""
    return str(raw.get("description") or raw.get("body") or "")
