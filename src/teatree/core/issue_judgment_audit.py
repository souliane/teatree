"""Private records of issue duplicate judgments."""

from typing import TYPE_CHECKING

from teatree.core.send_proxy import forge_from_url, record_private_audit

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from teatree.core.issue_hygiene import CreateDecision, CreateOutcome, IssueDraft


def record_issue_judgment(
    *,
    draft: "IssueDraft",
    outcome: "CreateOutcome",
    decisions: "Sequence[CreateDecision]",
    live_urls: "Collection[str]",
) -> None:
    live_decisions = [decision for decision in decisions if decision.candidate_url in live_urls]
    lines = [f"{outcome.kind} {outcome.issue_url}: {len(live_urls)} candidates judged"]
    for decision in live_decisions:
        verdict = "fits" if decision.fits else "rejected"
        reason = decision.reason.strip() or "caller marked as fitting"
        lines.append(f"{decision.candidate_url} {verdict}: {reason}")
    record_private_audit(
        forge=draft.forge or forge_from_url(outcome.issue_url),
        repo=draft.repo,
        action=f"{draft.action}_judgment",
        target=outcome.issue_url,
        text="\n".join(lines),
    )
