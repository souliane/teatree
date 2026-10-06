"""The published-message gate at the bound-merge chokepoint (BLUEPRINT §17.4.3 step 7).

A merge publishes the PR title and body as its commit message on the target's
default branch, where no later edit can scrub it. The message is scanned here
and the merge then sends exactly the scanned text, so the repo's commit-message
template can never publish anything the scan did not read.
"""

from dataclasses import replace

from teatree.core.backend_protocols import PrMessage
from teatree.core.gates.privacy_gate import BUILTIN_QUOTE_PATTERN_NAMES, format_refusal, scan_outbound_text
from teatree.core.merge.errors import MergePreconditionError
from teatree.hooks import quote_scanner
from teatree.hooks.ai_signature_scan import scan_text, summary
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
    text = message.as_text()
    if findings := scan_text(text):
        msg = f"{target}'s commit message carries an AI-signature trailer — {_REFUSAL}\n{summary(findings)}"
        raise MergePreconditionError(msg)
    privacy = scan_outbound_text(text=text, target_repo=ref.slug, forge=ref.host_kind)
    if not privacy.is_public:
        return message
    # Quotes are judged by the publish hook's structured detector, so a merge never refuses a body pr create admitted.
    leaks = replace(
        privacy, matches=tuple(m for m in privacy.matches if m.pattern_name not in BUILTIN_QUOTE_PATTERN_NAMES)
    )
    if leaks.refused:
        msg = f"{target}'s commit message fails the public-repo leak scan — {_REFUSAL}\n{format_refusal(leaks)}"
        raise MergePreconditionError(msg)
    if quotes := quote_scanner.scan_text(text).high:
        names = ", ".join(sorted({finding.name for finding in quotes}))
        msg = f"{target}'s commit message carries a user quote ({names}) — {_REFUSAL}"
        raise MergePreconditionError(msg)
    return message
