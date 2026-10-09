"""Put a PR on a ticket's ledger — the sweep refuses an own PR no ticket owns."""

from teatree.core.models import PullRequest, Ticket


def own_pr(slug: str, pr_id: int, *, overlay: str = "teatree") -> PullRequest:
    ticket, _ = Ticket.objects.get_or_create(overlay=overlay, issue_url=f"https://github.com/{slug}/issues/1")
    row, _ = PullRequest.objects.get_or_create(
        url=f"https://github.com/{slug}/pull/{pr_id}",
        defaults={"ticket": ticket, "overlay": overlay, "repo": slug, "iid": str(pr_id)},
    )
    return row
