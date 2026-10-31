"""The published-message gate at the bound-merge chokepoint (BLUEPRINT §17.4.3 step 7).

A merge publishes the PR title and body as its commit message on the target's
default branch, where no later edit can scrub it. The message is scanned here
and the merge then sends exactly the scanned text, so the repo's commit-message
template can never publish anything the scan did not read.
"""

from teatree.core.backend_protocols import PrMessage
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.models import PullRequest
from teatree.core.publishing.live_text import scan_live_text
from teatree.utils.pr_ref import PrRef

_REFUSAL = "refusing to merge. Edit the PR title/body, then re-run the merge; the CLEAR binds the head, not the body."


def scanned_merge_message(ref: PrRef, message: PrMessage | None) -> PrMessage:
    """*message* once it is proven publishable to *ref*; raise :class:`MergePreconditionError` otherwise."""
    target = f"{ref.slug}#{ref.pr_id}"
    if message is None:
        msg = (
            f"the title/body of {target} could not be read from the forge — refusing to merge: an unread "
            f"commit message cannot be proven free of AI signatures and private terms (§17.4.3 step 7)"
        )
        raise MergePreconditionError(msg)
    verdict = scan_live_text(message.as_text(), ref.slug, ref.host_kind, _own_issue_refs(ref))
    if verdict.clean:
        return message
    finding = verdict.findings[0]
    msg = f"{target}'s commit message {finding.kind.headline} — {_REFUSAL}\n{finding.detail}"
    raise MergePreconditionError(msg)


def _own_issue_refs(ref: PrRef) -> frozenset[str]:
    ticket = PullRequest.objects.owning_ticket(slug=ref.slug, pr_id=ref.pr_id)
    return frozenset({ticket.issue_url}) if ticket and ticket.issue_url else frozenset()
