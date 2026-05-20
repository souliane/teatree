"""Slack-recorded per-MR approval token for the live-post pre-gate (#1207).

``t3 review post-comment`` defaults to creating a draft. The ``--live``
flag asks the CLI to publish a colleague-visible comment directly; that
is only allowed when the user has just approved it in a Slack DM. The
:class:`LivePostApproval` row is the durable, MR-URL-scoped, single-use
token minted by ``t3 review approve-live-post`` after it verifies the
Slack message at ``--slack-ts``: from the user, recent, contains an
explicit approval phrase. Mirrors the #960 ``OnBehalfApproval`` shape:

* guarded factory :meth:`LivePostApproval.record` is the only way a row
    is written — it refuses an empty ``mr_url``/``slack_ts``;
* ``consumed_at`` makes every approval single-use — one live post per
    approval, never a standing authorization;
* ``mr_url`` strictly scopes the approval — an approval for !386 never
    authorizes a live post on !387;
* :attr:`LIVE_POST_APPROVAL_TTL_MINUTES` caps how long an unconsumed
    approval is valid (a 30-minute-old "go ahead" is stale).

The companion ``t3 review approve-live-post`` CLI command is the
satisfier; the gate helper ``require_live_post_approval`` consumes the
row on the next matching ``--live`` post.
"""

from datetime import timedelta
from typing import ClassVar
from urllib.parse import urlparse

from django.db import models, transaction
from django.utils import timezone

LIVE_POST_APPROVAL_TTL_MINUTES = 15


def canonical_mr_scope(mr_url_or_ref: str) -> str:
    """Return the canonical scope key for a merge-request reference.

    Accepts any of the three forms an agent may pass and rewrites them to
    a stable ``<repo>!<iid>`` token so an approval recorded for the URL
    matches a ``post-comment --live`` invocation made with ``<repo>``
    plus ``<iid>``:

    * ``"https://gitlab.com/org/proj/-/merge_requests/42"`` →
        ``"org/proj!42"``
    * ``"https://github.com/owner/repo/pull/17"`` →
        ``"owner/repo!17"``
    * ``"org/proj!42"`` (already canonical) → ``"org/proj!42"``

    An unrecognised string is returned stripped of whitespace — callers
    that need stricter shapes validate downstream.
    """
    raw = mr_url_or_ref.strip()
    if not raw:
        return ""
    if "://" not in raw:
        return raw
    path = urlparse(raw).path.strip("/")
    parts = path.split("/")
    # GitLab uses the ``/-/merge_requests/<iid>`` segment; GitHub uses
    # ``/pull/<iid>``.  Both end in ``<owner>/.../<segment>/<iid>``.
    if "merge_requests" in parts:
        idx = parts.index("merge_requests")
    elif "pull" in parts:
        idx = parts.index("pull")
    else:
        return raw
    if idx < 1 or idx + 1 >= len(parts):
        return raw
    iid = parts[idx + 1]
    repo_parts = parts[:idx]
    # GitLab nests groups; strip the trailing ``-`` segment if present.
    if repo_parts and repo_parts[-1] == "-":
        repo_parts = repo_parts[:-1]
    if not repo_parts:
        return raw
    return f"{'/'.join(repo_parts)}!{iid}"


class LivePostApprovalError(ValueError):
    """A ``LivePostApproval`` was rejected at record time — the contract failed."""


class LivePostApproval(models.Model):
    """One Slack-recorded user authorization for exactly one ``--live`` post on one MR.

    Mirrors :class:`~teatree.core.models.on_behalf_approval.OnBehalfApproval`
    (#960): durable row, single-use (``consumed_at``), strictly scoped
    (``mr_url``), creatable only through the guarded :meth:`record`
    factory. The Slack ``ts`` is preserved on the row as audit evidence
    of which DM authorized the post.
    """

    mr_url = models.CharField(max_length=512)
    slack_ts = models.CharField(max_length=64)
    slack_user_id = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_live_post_approval"
        ordering: ClassVar = ["-created_at"]

    def __str__(self) -> str:
        return f"live-post-approval<{self.mr_url} ts={self.slack_ts}>"

    @classmethod
    def record(cls, *, mr_url: str, slack_ts: str, slack_user_id: str) -> "LivePostApproval":
        """The single guarded factory for a recorded live-post approval.

        Enforces the contract before any row is written: non-empty
        ``mr_url``, ``slack_ts``, ``slack_user_id``. Caller is responsible
        for verifying the Slack DM authenticity (author, recency, phrase)
        BEFORE invoking ``record``; this factory only enforces the data
        invariants, not the Slack-side checks.
        """
        clean_mr_url = canonical_mr_scope(mr_url)
        if not clean_mr_url:
            msg = "mr_url is required and must be non-empty (#1207)"
            raise LivePostApprovalError(msg)

        clean_ts = slack_ts.strip()
        if not clean_ts:
            msg = "slack_ts is required and must be non-empty (#1207)"
            raise LivePostApprovalError(msg)

        clean_user = slack_user_id.strip()
        if not clean_user:
            msg = "slack_user_id is required and must be non-empty (#1207)"
            raise LivePostApprovalError(msg)

        with transaction.atomic():
            return cls.objects.create(
                mr_url=clean_mr_url,
                slack_ts=clean_ts,
                slack_user_id=clean_user,
            )

    @classmethod
    def consume(cls, *, mr_url: str) -> "LivePostApproval | None":
        """Atomically claim and consume the matching unconsumed approval, if any.

        Returns the consumed row (so the caller can proceed with the
        ``--live`` post) or ``None`` when no valid, fresh, unconsumed
        approval exists for this exact ``mr_url`` — the caller then
        refuses the live post and points the user at ``approve-live-post``.

        Stale rows (older than :data:`LIVE_POST_APPROVAL_TTL_MINUTES`)
        are treated as absent. ``select_for_update`` + the ``consumed_at``
        stamp make the claim single-use under concurrency.
        """
        clean_mr_url = canonical_mr_scope(mr_url)
        cutoff = timezone.now() - timedelta(minutes=LIVE_POST_APPROVAL_TTL_MINUTES)
        with transaction.atomic():
            row = (
                cls.objects.select_for_update()
                .filter(
                    mr_url=clean_mr_url,
                    consumed_at__isnull=True,
                    created_at__gte=cutoff,
                )
                .order_by("-created_at")
                .first()
            )
            if row is None:
                return None
            row.consumed_at = timezone.now()
            row.save(update_fields=["consumed_at"])
            return row
