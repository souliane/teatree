"""Per-(repo, ticket) open-PR budget gate — the north-star PR-2 proof-case mechanism.

The generic core shape a plain-language max-one-MR-per-repo directive resolves
to: a durable ``UserSettings`` knob read as data at the one core seam every
overlay's PR creation flows through, never an overlay-local branch in core.
Enforced before a PR is opened by the ``_run_ship_gates`` chain (``pr create``)
and the orphan-branch path (``ensure-pr``).

Constraint-as-data: ``max_open_prs_per_repo_per_ticket`` is read through
``get_effective_settings`` so its scope is a per-overlay ``ConfigSetting`` row —
a second overlay wanting a different value needs no code change (the N=2 litmus).
Neutral default = inert: the default ``0`` means unlimited, so core ships with
zero behaviour change until an overlay sets the knob (the empty-table doctrine);
``1`` gives at most one open PR per repo per ticket.

Exactness caveat: ``PullRequest`` rows are upserted by the manual-PR reconciler,
which can lag a just-opened PR by a tick. The count therefore UNIONS the FK rows
with ``ticket.extra["pr_url_by_branch"]`` (written synchronously by the ship
executor), so a second ``pr create`` fired before reconciliation still sees the
first PR.
"""

from teatree.config import get_effective_settings
from teatree.core.models import PullRequest, Ticket
from teatree.utils.url_slug import pr_ref_from_url


class PrBudgetExceededError(RuntimeError):
    """Refusal raised when opening another PR would breach the (repo, ticket) budget."""


def resolve_pr_budget(overlay: str | None) -> int:
    """Resolve the effective ``max_open_prs_per_repo_per_ticket`` for *overlay*.

    Reads through ``get_effective_settings`` so the value layers ``T3_*`` env,
    the per-overlay ``ConfigSetting`` row, the global row, then the dataclass
    default in order. ``0`` (the neutral default) means unlimited.
    """
    return int(get_effective_settings(overlay).max_open_prs_per_repo_per_ticket)


def open_pr_urls_for_repo(ticket: Ticket, repo_slug: str) -> set[str]:
    """Return the distinct OPEN (not-merged) PR URLs for exactly ``(ticket, repo_slug)``.

    Two sources are unioned (deduped by URL): the ticket's non-merged
    ``PullRequest`` rows in *repo_slug*, and any ``extra["pr_url_by_branch"]``
    URL whose parsed repo slug is *repo_slug* — the synchronously-written index
    that catches a PR the FK reconciler has not yet upserted.
    """
    urls: set[str] = set(
        PullRequest.objects.filter(ticket=ticket, repo=repo_slug)
        .exclude(state=PullRequest.State.MERGED)
        .values_list("url", flat=True),
    )
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    by_branch = extra.get("pr_url_by_branch")
    if isinstance(by_branch, dict):
        for url in by_branch.values():
            if not isinstance(url, str):
                continue
            ref = pr_ref_from_url(url)
            if ref is not None and ref.slug == repo_slug:
                urls.add(url)
    return urls


def count_open_prs_for_repo(ticket: Ticket, repo_slug: str) -> int:
    """Count the distinct open (not-merged) PR URLs for exactly ``(ticket, repo_slug)``."""
    return len(open_pr_urls_for_repo(ticket, repo_slug))


def check_pr_budget(ticket: Ticket, repo_slug: str, *, limit: int | None = None) -> None:
    """Refuse the impending PR when ``(ticket, repo_slug)`` is at its open-PR budget.

    Inert at the neutral default: a non-positive *limit* returns immediately, so
    core is unchanged until an overlay opts in. Otherwise, when the count of open
    PRs for exactly this ``(ticket, repo_slug)`` already meets or exceeds *limit*,
    raise :class:`PrBudgetExceededError` naming the offending PR URLs and the
    operator escape. *limit* defaults to the effective per-overlay setting;
    passing it explicitly is the test seam.
    """
    effective_limit = resolve_pr_budget(ticket.overlay or None) if limit is None else limit
    if effective_limit <= 0:
        return
    urls = open_pr_urls_for_repo(ticket, repo_slug)
    if len(urls) < effective_limit:
        return
    overlay = ticket.overlay or "<overlay>"
    reference = ticket.ticket_number or str(ticket.pk)
    offending = "\n  - ".join(sorted(urls))
    msg = (
        f"max_open_prs_per_repo_per_ticket={effective_limit} for overlay {overlay!r} "
        f"would be exceeded: ticket {reference} already has {len(urls)} open PR(s) in "
        f"{repo_slug!r}:\n  - {offending}\n"
        f"Merge or close an existing PR, or lift the cap: "
        f"`t3 {overlay} config_setting set max_open_prs_per_repo_per_ticket 0 --overlay {overlay}`."
    )
    raise PrBudgetExceededError(msg)
