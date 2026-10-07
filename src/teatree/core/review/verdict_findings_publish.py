"""Publish a recorded verdict's findings to the PR they were reached on (#4476, #4968).

Routing is by who authored the PR. A self-authored PR gets nothing — no comment, no DM: its
findings are fix work for the factory, never a post. A PR whose author cannot be read is
withheld the same way. A colleague PR gets ONE submitted inline review: each anchored finding
on its own line, the rest in the summary. Every body first passes the same comment checks as
``review post-comment`` (:mod:`teatree.core.review.comment_checks`) — one failure withholds the
whole review — then the two gates every colleague-visible forge body must pass:
:func:`~teatree.core.send_proxy.route_forge_write` (public-repo leak scan + send-proxy
audit/allowlist) and the on-behalf pre-gate.

An on-behalf block DMs the owner the findings, so the block can never also hide the content.
An unresolvable backend, an unreadable diff, a forge refusal, a partial post, and a review that
does not read back all raise :class:`FindingsPublishError`.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import cache
from typing import TYPE_CHECKING

from teatree.core.backend_protocols import PartialReviewPublishError, PrReview
from teatree.core.checking import build_pr_url
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.core.review.comment_checks import review_refusal
from teatree.core.review.review_candidate import is_self_authored
from teatree.core.review.verdict_findings import findings_payload, marker_for, render_findings_text, review_for
from teatree.core.self_forge_identities import self_identity_set

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
    self_review: bool = False
    blocked_reason: str = ""
    note: str = ""


def publish_verdict_findings(
    verdict: ReviewVerdict,
    *,
    host_kind: str,
    backend: "CodeHostBackend | None" = None,
) -> PublishOutcome:
    """Submit *verdict*'s findings as one inline review on its colleague PR, or report why not.

    Idempotent by the hidden marker: a re-run finds the verdict's own review and skips.
    An empty *host_kind* withholds: a bare slug names no forge, and a guessed one misreads the author.
    """
    if not findings_payload(verdict):
        return PublishOutcome(note=f"verdict {verdict.pk} carries no findings — nothing to publish")
    if not host_kind:
        return PublishOutcome(blocked_reason=f"the forge hosting {verdict.slug} is unknown — findings withheld")

    host = backend if backend is not None else _resolve_backend(verdict)
    target = f"{verdict.slug}#{verdict.pr_id}"
    stop = _stop_before_posting(host, verdict, host_kind=host_kind, target=target)
    if stop is not None:
        return stop

    review = review_for(verdict)
    refusal = review_refusal(review, file_diffs=_diff_reader(host, verdict, target=target))
    if refusal:
        return PublishOutcome(blocked_reason=f"comment check — {refusal}")
    outcome = _submit_gated(host, verdict, review=_scrubbed(review, verdict, host_kind=host_kind, target=target))
    if outcome.published:
        _confirm_landed(host, verdict, target=target)
    return outcome


def _stop_before_posting(
    host: "CodeHostBackend", verdict: ReviewVerdict, *, host_kind: str, target: str
) -> PublishOutcome | None:
    own = _is_own_pr(host, verdict, host_kind=host_kind)
    if own is True:
        return PublishOutcome(
            self_review=True, note=f"self-authored PR {target} — findings are fix work, nothing posted"
        )
    if own is None:
        return PublishOutcome(blocked_reason=f"the author of {target} could not be read — findings withheld")
    if _already_published(host, verdict):
        return PublishOutcome(skipped_existing=True, note=f"findings already posted on {target}")
    live_head = host.fetch_live_head_sha(slug=verdict.slug, pr_id=int(verdict.pr_id))
    if live_head != verdict.reviewed_sha:
        return PublishOutcome(
            blocked_reason=f"head moved: reviewed {verdict.reviewed_sha[:8]}, live {live_head[:8] or 'unreadable'}"
        )
    return None


def _diff_reader(host: "CodeHostBackend", verdict: ReviewVerdict, *, target: str) -> Callable[[], dict[str, str]]:
    @cache
    def read() -> dict[str, str]:
        try:
            return host.get_pr_file_diffs(repo=verdict.slug, pr_iid=int(verdict.pr_id))
        except Exception as exc:
            msg = f"could not read the diff of {target} for the TODO-anchor check — findings withheld"
            raise FindingsPublishError(msg) from exc

    return read


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


def _already_published(host: "CodeHostBackend", verdict: ReviewVerdict) -> bool:
    """Whether the PR already carries this verdict's review.

    A read failure is NOT treated as "no review yet": posting a duplicate on an
    unreadable list is the worse outcome, so it fails loud.
    """
    try:
        return host.find_pr_review(repo=verdict.slug, pr_iid=int(verdict.pr_id), marker=marker_for(verdict))
    except Exception as exc:
        msg = f"could not read existing reviews on {verdict.slug}#{verdict.pr_id} — refusing to risk a duplicate post"
        raise FindingsPublishError(msg) from exc


def _confirm_landed(host: "CodeHostBackend", verdict: ReviewVerdict, *, target: str) -> None:
    unconfirmed = f"the review submitted to {target} does not read back — check the PR before a retry"
    try:
        landed = host.find_pr_review(repo=verdict.slug, pr_iid=int(verdict.pr_id), marker=marker_for(verdict))
    except Exception as exc:
        raise FindingsPublishError(unconfirmed) from exc
    if not landed:
        raise FindingsPublishError(unconfirmed)


def _is_own_pr(host: "CodeHostBackend", verdict: ReviewVerdict, *, host_kind: str) -> bool | None:
    url = build_pr_url(slug=verdict.slug, pr_id=int(verdict.pr_id), code_host=host_kind)
    if not url:
        return None
    return is_self_authored(url, host, self_identity_set(url, host=host))


def _scrubbed(review: PrReview, verdict: ReviewVerdict, *, host_kind: str, target: str) -> PrReview:
    from teatree.core.send_proxy import route_forge_write  # noqa: PLC0415 — deferred: keeps the import light

    def scrub(text: str) -> str:
        return route_forge_write(forge=host_kind, repo=verdict.slug, text=text, action=ACTION, target=target)

    return replace(
        review,
        body=scrub(review.body),
        comments=tuple(replace(item, body=scrub(item.body)) for item in review.comments),
    )


def _submit_gated(host: "CodeHostBackend", verdict: ReviewVerdict, *, review: PrReview) -> PublishOutcome:
    from teatree.core.on_behalf_gate_recorded import (  # noqa: PLC0415 — deferred: keeps the import light
        OnBehalfPartialPublishError,
        OnBehalfPostBlockedError,
        require_on_behalf_approval,
    )

    target = f"{verdict.slug}#{verdict.pr_id}"

    def _publish() -> "RawAPIDict":
        try:
            posted = host.submit_pr_review(repo=verdict.slug, pr_iid=int(verdict.pr_id), review=review)
        except PartialReviewPublishError as exc:
            raise OnBehalfPartialPublishError((f"the findings review on {target} partly posted: {exc}", 1)) from exc
        except Exception as exc:
            msg = f"the forge refused the findings review on {target}: {exc}"
            raise FindingsPublishError(msg) from exc
        if posted.get("error"):
            msg = f"the forge refused the findings review on {target}: {posted['error']}"
            raise FindingsPublishError(msg)
        return posted

    try:
        posted = require_on_behalf_approval(target=target, action=ACTION, publish=_publish)
    except OnBehalfPostBlockedError as exc:
        _dm_withheld_findings(verdict, target)
        return PublishOutcome(blocked_reason=str(exc))
    except OnBehalfPartialPublishError as exc:
        msg = f"{exc} — a retry re-posts what landed, since the marker rides the last post"
        raise FindingsPublishError(msg) from exc
    return PublishOutcome(published=True, comment_url=_comment_url(posted, target))


def _comment_url(posted: "RawAPIDict", target: str) -> str:
    url = posted.get("html_url") or posted.get("web_url")
    return str(url) if url else target


def _dm_withheld_findings(verdict: ReviewVerdict, target: str) -> None:
    """DM the owner the findings the on-behalf gate withheld — the block must not also hide them.

    Best-effort: a messaging outage must not turn a gate block into a crash that
    loses the block reason the caller is about to report.
    """
    from teatree.core.modelkit.notify_policy import NotifyAudience  # noqa: PLC0415 — deferred
    from teatree.core.notify import NotifyKind, notify_user  # noqa: PLC0415 — deferred: keeps the import light

    try:
        notify_user(
            f"Findings for {target} were NOT posted (on-behalf gate).\n{render_findings_text(verdict)}",
            kind=NotifyKind.INFO,
            idempotency_key=f"review-findings-blocked:{verdict.pk}",
            audience=NotifyAudience.OWNER_DELIVERY,
        )
    except Exception:  # noqa: BLE001 — the DM is the fallback channel, never the failure mode
        return
