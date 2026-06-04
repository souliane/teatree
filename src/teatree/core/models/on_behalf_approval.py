"""Recorded per-post user-approval channel for the on-behalf pre-gate (#960/#961).

The ``on_behalf_post_mode`` pre-gate (``teatree.on_behalf_gate``)
refuses to publish a post made under the user's identity to a
colleague/customer surface (BLOCK verdict under ``ask`` / ``draft_or_ask``)
unless the user has approved it. Its only
satisfier must NOT be an interactive TTY — a chat-only operator plus any
unattended loop could then never let a sanctioned reply go out, the exact
``#777``/``#953`` anti-pattern.

``OnBehalfApproval`` is the parallel of the already-validated #953
``DbApproval`` (which itself mirrors ``MergeClear`` / BLUEPRINT §17.4):

* guarded factory :meth:`OnBehalfApproval.record` is the only way a row is
    written;
* a maker/coding-agent/loop approver id is refused (~ the maker!=checker
    ``is_non_reviewer_role()`` guard) — the executing agent can never
    self-authorize the post it is about to make;
* ``consumed_at`` makes every approval single-use, per-post — no standing
    approval survives one post;
* ``target`` + ``action`` strictly scope the approval — it authorizes that
    one post (e.g. ``post_comment`` on ``org/repo#42``) and nothing else;
* :class:`OnBehalfAudit` (~ ``DbAudit`` / ``MergeAudit``) is the
    post-publication audit row: who approved, which target, which action,
    when.

A user satisfies the gate by recording one of these rows
(``t3 review approve-on-behalf <target> <action> --approver <id>``) — no
terminal required; the agent then publishes and the row is consumed. With
no recorded approval the gate does NOT post: it surfaces the blocked post
to the user (the user-notify path) so the user can approve in plain text.
"""

from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.live_post_approval import canonical_mr_scope
from teatree.core.models.merge_clear import is_non_reviewer_role


def canonical_on_behalf_target(target: str) -> str:
    """Return the stable scope key an on-behalf approval is recorded/consumed under.

    The on-behalf gate is the single chokepoint for every post made under
    the user's identity, but each consume call site builds the target token
    differently for the *same* merge request: ``review_on_behalf`` and
    ``pr.py`` use ``"{repo}!{iid}"``; ``review_request_post`` and the signals
    approval-reaction use the full MR/PR URL. With a strict exact-string
    match a legitimately pre-recorded approval in one form silently failed to
    match a consume token built in another — the documented PRE-RECORD
    workflow over-denied.

    This normalizes every form to the same token at BOTH record and consume:

    * an MR/PR URL (``https://.../-/merge_requests/<iid>`` or
        ``https://.../pull/<iid>``) → ``"<repo>!<iid>"`` (delegated to
        :func:`~teatree.core.models.live_post_approval.canonical_mr_scope`,
        the same canonicalizer the #1207 live-post gate uses);
    * an already-canonical ``"<repo>!<iid>"`` → unchanged;
    * any non-MR target (an issue URL, a ``ticket:<pk>`` compound, a Slack
        ``channel/thread`` ref) → returned stripped of whitespace only, so a
        non-MR scope is preserved verbatim and never collapsed.
    """
    return canonical_mr_scope(target)


class OnBehalfApprovalError(ValueError):
    """An ``OnBehalfApproval`` was rejected at record time — the contract failed."""


class OnBehalfApproval(models.Model):
    """One recorded user authorisation for exactly one target+action on-behalf post.

    Mirrors ``DbApproval`` (#953) / ``MergeClear`` (§17.4.2): a durable row,
    single-use (``consumed_at``), strictly scoped (``target`` + ``action``),
    creatable only through the guarded :meth:`record` factory which refuses
    a self/agent/loop approver. A consumed or scope-mismatched row is
    treated as absent by :meth:`matches`.
    """

    target = models.CharField(max_length=512)
    action = models.CharField(max_length=64)
    approver_id = models.CharField(max_length=255)
    created_at = models.DateTimeField(default=timezone.now)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_on_behalf_approval"
        ordering: ClassVar = ["-created_at"]

    def __str__(self) -> str:
        return f"on-behalf-approval<{self.action}:{self.target} by {self.approver_id}>"

    @classmethod
    def record(cls, target: str, action: str, approver_id: str) -> "OnBehalfApproval":
        """The single guarded factory for a recorded per-post approval.

        Enforces the contract before any row is written and raises
        :class:`OnBehalfApprovalError` with a precise reason on the first
        violation: non-empty ``target``/``action``/``approver_id``; an
        ``approver_id`` that is NOT a maker/coding-agent/loop role (the
        executing agent can never self-authorize the post — ≈ the
        maker≠checker ``is_non_reviewer_role()`` guard on
        ``MergeClear.issue`` / ``DbApproval.record``). Construction is
        atomic so a rejected approval leaves no partial row.
        """
        clean_target = canonical_on_behalf_target(target)
        if not clean_target:
            msg = "target is required and must be non-empty (#960)"
            raise OnBehalfApprovalError(msg)

        clean_action = action.strip()
        if not clean_action:
            msg = "action is required and must be non-empty (#960)"
            raise OnBehalfApprovalError(msg)

        approver = approver_id.strip()
        if not approver:
            msg = "approver_id is required and must be non-empty (#960)"
            raise OnBehalfApprovalError(msg)
        if is_non_reviewer_role(approver):
            msg = (
                f"approver_id {approver!r} is a maker/coding-agent/loop role — an "
                f"OnBehalfApproval must be recorded by a user, never self-authorized "
                f"by the executing agent (#960, mirrors DbApproval #953 / MergeClear §17.8)"
            )
            raise OnBehalfApprovalError(msg)

        with transaction.atomic():
            return cls.objects.create(target=clean_target, action=clean_action, approver_id=approver)

    def matches(self, target: str, action: str) -> bool:
        """True iff this row is unconsumed and scoped to exactly *target* + *action*.

        A consumed approval is single-use and no longer matches (reusing it
        would let a replay slip a second unapproved post through). The scope
        is exact under canonicalization: an approval recorded for an MR in
        any surface form (URL or ``<repo>!<iid>``) matches a consume token
        for the SAME MR in any other form, but never a different MR or
        action (see :func:`canonical_on_behalf_target`).
        """
        if self.consumed_at is not None:
            return False
        return self.target == canonical_on_behalf_target(target) and self.action == action.strip()

    @classmethod
    def has_unconsumed(cls, target: str, action: str, *, using: str | None = None) -> bool:
        """True iff an unconsumed approval is recorded for exactly *target* + *action*.

        The read-only peek behind
        :func:`~teatree.core.on_behalf_gate_recorded.on_behalf_block_message`:
        it reports whether a consume *would* succeed without claiming the row,
        so a caller can refuse a blocked post early before doing expensive
        prep. It never stamps ``consumed_at`` — the actual single-use claim
        stays inside :meth:`consume`, run atomically with the post.
        """
        clean_target = canonical_on_behalf_target(target)
        clean_action = action.strip()
        manager = cls.objects.using(using) if using else cls.objects
        return manager.filter(target=clean_target, action=clean_action, consumed_at__isnull=True).exists()

    @classmethod
    def consume(cls, target: str, action: str, *, using: str | None = None) -> "OnBehalfApproval | None":
        """Atomically claim and consume the matching unconsumed approval, if any.

        Returns the consumed row (so the caller can write the audit) or
        ``None`` when no valid recorded approval exists for this exact
        target+action — the caller then surfaces the blocked post to the
        user instead of publishing. The ``consumed_at`` stamp +
        ``select_for_update`` make the claim single-use even under a
        concurrent second post on the same target+action.

        ``using`` selects an alternate Django database alias for the read,
        the locked re-read and the consume write — used by the concurrent
        regression test (``test_on_behalf_approval_concurrent.py``) to point
        consume at a file-backed SQLite registered with prod's
        ``transaction_mode=IMMEDIATE`` ``OPTIONS``. Production callers pass
        no ``using`` and run against the default connection.
        """
        clean_target = canonical_on_behalf_target(target)
        clean_action = action.strip()
        manager = cls.objects.using(using) if using else cls.objects
        with transaction.atomic(using=using):
            row = (
                manager.select_for_update()
                .filter(target=clean_target, action=clean_action, consumed_at__isnull=True)
                .order_by("created_at")
                .first()
            )
            if row is None:
                return None
            row.consumed_at = timezone.now()
            row.save(update_fields=["consumed_at"], using=using)
            return row


class OnBehalfAudit(models.Model):
    """Post-publication audit of a recorded-approval on-behalf post (#960).

    ≈ ``DbAudit`` (#953) / ``MergeAudit`` (§17.4): who approved, which
    target, which action, when the post actually went out.
    """

    approval = models.ForeignKey(
        OnBehalfApproval,
        on_delete=models.CASCADE,
        related_name="audits",
    )
    target = models.CharField(max_length=512)
    action = models.CharField(max_length=64)
    approver_id = models.CharField(max_length=255)
    executed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_on_behalf_audit"
        ordering: ClassVar = ["-executed_at"]

    def __str__(self) -> str:
        return f"on-behalf-audit<{self.action}:{self.target} by {self.approver_id}>"
