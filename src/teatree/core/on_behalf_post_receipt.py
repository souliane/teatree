"""After-receipt notifier for colleague-visible on-behalf posts (#949).

The companion of the *pre*-gate :mod:`teatree.core.on_behalf_gate_recorded`
(which decides whether a post may publish). This module fires *after* a
colleague-visible post has already published successfully — it DMs the
user the destination, a clickable artifact link, and a one-line summary
so every post made under their identity is visible to them.

Distinct concern from the pre-gate (kept in its own module so the
pre-gate stays pure): the pre-gate may BLOCK; this never can — the post
already happened, the caller already published. So the failure model is
**record-and-proceed**: attempt the notify, durably record its outcome
via :func:`teatree.core.notify.notify_user` (which never raises into the
caller and writes a ``BotPing`` FAILED/NOOP + ``OutboundClaim`` row on
non-delivery so the audit scanner re-DMs on drift), then return. Never
raise, never fail or roll back the post — a colleague comment cannot be
un-posted.

Gated by the default-ON ``notify_on_post_on_behalf`` UserSettings field.
When it resolves false the DM is suppressed (the post still happened and
the caller already published); per-overlay overridable, no env var.

Depends only on :mod:`teatree.config` and :mod:`teatree.core.notify` —
both already legal ``teatree.core`` edges, so no new tach edge.
"""

from teatree.config import get_effective_settings


def notify_user_on_behalf_post(
    *,
    target: str,
    action: str,
    destination: str,
    artifact_url: str,
    summary: str,
) -> None:
    """DM the user that a colleague-visible post published under their identity.

    ``target``/``action`` form the idempotency key
    ``on_behalf_post:{target}:{action}`` — one DM per (target, action)
    across retries (mirrors the ``on_behalf_autodraft:`` convention).
    ``destination`` is the human-readable place the post landed (a review
    channel, an ``org/repo!7`` ref). ``artifact_url`` is the clickable
    permalink/URL of the post; ``notify_user``'s ``maybe_linkify``
    converts the ``[label](url)`` form to Slack ``<url|label>``.
    ``summary`` is the one-line description of what was posted.

    Suppressed (early return, no DM) only when BOTH the user-facing
    ``notify_on_post_on_behalf`` toggle (#949) AND the autonomy-derived
    ``notify_on_behalf`` (the ``notify`` tier's forced DM, #1668) resolve
    false — the post already happened and the caller already published;
    this only controls the after-receipt visibility DM. The ``notify``
    autonomy tier drives ``notify_on_behalf = True``, so its on-behalf
    actions always DM the user through this one canonical egress regardless
    of the #949 toggle, without adding a parallel notifier.

    Never raises into the caller and never fails or rolls back the post:
    ``notify_user`` already wraps every transport failure into a
    NOOP/FAILED ``BotPing`` row (the audit scanner re-DMs on drift), so
    a misconfigured Slack backend cannot break a legitimate publish.
    """
    settings = get_effective_settings()
    if not (settings.notify_on_post_on_behalf or settings.notify_on_behalf):
        return

    from teatree.core.notify import NotifyKind, notify_user  # noqa: PLC0415 — deferred: call-time import, kept lazy

    short = artifact_url.rsplit("/", 1)[-1] or artifact_url
    text = f"Posted under your identity to {destination}.\n[{short}]({artifact_url})\n{summary}"
    notify_user(
        text,
        kind=NotifyKind.INFO,
        idempotency_key=f"on_behalf_post:{target}:{action}",
    )
