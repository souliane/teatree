"""Publish a recorded verdict's findings to the PR they were reached on (#4476).

A HOLD is only actionable where the work is. This is the write half of
:mod:`teatree.core.review.verdict_findings`: it posts the rendered findings as
one PR comment, through the two gates every colleague-visible forge body must
pass — :func:`~teatree.core.send_proxy.route_forge_write` (public-repo leak scan
+ send-proxy audit/allowlist) and the on-behalf pre-gate.

Nothing here degrades a failure to silence. An unresolvable backend raises; a
blocked post returns the block reason AND DMs the owner the findings, so the
gate withholding the comment can never also hide its content.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.core.models.review_verdict import ReviewVerdict
from teatree.core.review.verdict_findings import (
    comment_carries_marker,
    findings_payload,
    marker_for,
    render_findings_markdown,
)

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend
    from teatree.types import RawAPIDict

ACTION = "post_review_findings"
"""The on-behalf action name this publish is gated under.

Deliberately NOT in the shipped ``on_behalf_auto_actions`` default: on a
customer overlay a review critique posted under the user's name is a colleague
voice, not self-documentation. The owner opts in per overlay, or approves once.
"""


class FindingsPublishError(RuntimeError):
    """The findings could not be published — the caller must not read this as "nothing to post"."""


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """What one publish attempt did — every non-published case names its own reason."""

    published: bool = False
    comment_url: str = ""
    skipped_existing: bool = False
    blocked_reason: str = ""
    note: str = ""


def publish_verdict_findings(
    verdict: ReviewVerdict,
    *,
    host_kind: str = "github",
    backend: "CodeHostBackend | None" = None,
) -> PublishOutcome:
    """Post *verdict*'s findings to its PR, or report why it did not.

    Idempotent by the hidden marker: a re-run finds the verdict's own comment
    and skips rather than posting a duplicate.
    """
    if not findings_payload(verdict):
        return PublishOutcome(note=f"verdict {verdict.pk} carries no findings — nothing to publish")

    host = backend if backend is not None else _resolve_backend(verdict)
    target = f"{verdict.slug}#{verdict.pr_id}"
    marker = marker_for(verdict)
    if _already_published(host, verdict, marker):
        return PublishOutcome(skipped_existing=True, note=f"findings already posted on {target}")

    body = _scrubbed_body(verdict, host_kind=host_kind, target=target)
    return _post_gated(host, verdict, body=body, target=target)


def _resolve_backend(verdict: ReviewVerdict) -> "CodeHostBackend":
    from teatree.core.backend_factory import code_host_from_overlay  # noqa: PLC0415 — deferred: keeps the import light

    host = code_host_from_overlay()
    if host is None:
        msg = (
            f"no code-host backend resolved for {verdict.slug}#{verdict.pr_id} — the findings cannot reach the PR. "
            f"Configure the overlay's forge credential, or read them with `t3 <overlay> review findings <pr-url>`"
        )
        raise FindingsPublishError(msg)
    return host


def _already_published(host: "CodeHostBackend", verdict: ReviewVerdict, marker: str) -> bool:
    """Whether the PR already carries this verdict's findings comment.

    A read failure is NOT treated as "no comment yet": posting a duplicate on an
    unreadable list is the worse outcome, so it fails loud.
    """
    try:
        existing = host.list_pr_comments(repo=verdict.slug, pr_iid=int(verdict.pr_id))
    except Exception as exc:
        msg = f"could not read existing comments on {verdict.slug}#{verdict.pr_id} — refusing to risk a duplicate post"
        raise FindingsPublishError(msg) from exc
    return any(comment_carries_marker(comment, marker) for comment in existing)


def _scrubbed_body(verdict: ReviewVerdict, *, host_kind: str, target: str) -> str:
    from teatree.core.send_proxy import route_forge_write  # noqa: PLC0415 — deferred: keeps the import light

    return route_forge_write(
        forge=host_kind,
        repo=verdict.slug,
        text=render_findings_markdown(verdict),
        action=ACTION,
        target=target,
    )


def _post_gated(host: "CodeHostBackend", verdict: ReviewVerdict, *, body: str, target: str) -> PublishOutcome:
    from teatree.core.on_behalf_gate_recorded import (  # noqa: PLC0415 — deferred: keeps the import light
        OnBehalfPostBlockedError,
        require_on_behalf_approval,
    )

    def _publish() -> "RawAPIDict":
        return host.post_pr_comment(repo=verdict.slug, pr_iid=int(verdict.pr_id), body=body)

    try:
        posted = require_on_behalf_approval(target=target, action=ACTION, publish=_publish)
    except OnBehalfPostBlockedError as exc:
        _dm_withheld_findings(verdict, target)
        return PublishOutcome(blocked_reason=str(exc))
    return PublishOutcome(published=True, comment_url=_comment_url(posted, target))


def _comment_url(posted: "RawAPIDict", target: str) -> str:
    url = posted.get("html_url") or posted.get("web_url")
    return str(url) if url else target


def _dm_withheld_findings(verdict: ReviewVerdict, target: str) -> None:
    """DM the owner the findings the gate withheld — the block must not also hide them.

    Best-effort: a messaging outage must not turn a gate block into a crash that
    loses the block reason the caller is about to report.
    """
    from teatree.core.modelkit.notify_policy import NotifyAudience  # noqa: PLC0415 — deferred
    from teatree.core.notify import NotifyKind, notify_user  # noqa: PLC0415 — deferred: keeps the import light
    from teatree.core.review.verdict_findings import render_findings_text  # noqa: PLC0415 — deferred

    try:
        notify_user(
            f"Findings for {target} were NOT posted (on-behalf gate).\n{render_findings_text(verdict)}",
            kind=NotifyKind.INFO,
            idempotency_key=f"review-findings-blocked:{verdict.pk}",
            audience=NotifyAudience.OWNER_DELIVERY,
        )
    except Exception:  # noqa: BLE001 — the DM is the fallback channel, never the failure mode
        return
