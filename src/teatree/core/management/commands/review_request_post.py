"""``t3 review-request post`` — sanctioned authorized review-request post (#1098).

The post-half of #1084/#1094. One classifier-legible transaction:

1.  ``resolve_guard_target`` — resolves the postable review channel. When it
    returns ``None`` (e.g. a Slack Connect channel the bot token cannot post
    to — #2231), a bot→user DM draft is sent via ``notify_user`` and the
    command exits with ``action=draft`` / ``reason=no_review_channel_or_token``
    (exit 0). No channel post is made; no dedup claim is taken.
2.  #1094 ``review_request_guard`` live-channel dedup
    (``should_post_review_request`` — takes the atomic ``ReviewRequestPost``
    claim internally). ``suppress`` → no post.
3.  #960 ``require_on_behalf_approval`` — the single chokepoint. No recorded,
    unconsumed, exactly-scoped ``OnBehalfApproval`` → ``OnBehalfPostBlockedError``
    (its ``str`` already names the exact ``t3 review approve-on-behalf``
    remediation). On that refusal the just-created guard claim is rolled
    back (Risk-c: an orphan claim would make every future legitimate post
    suppress with ``already_claimed`` forever).
4.  Only then post to the review channel, persist the permalink record.

``action``/``target`` are the canonical strings, derived once via
``canonical_mr_url`` so the dedup claim and the #960 approval scope are
provably the same string.
"""

from typing import Annotated, NoReturn

import typer
from django_typer.management import TyperCommand, command

from teatree.core.backend_factory import messaging_from_overlay
from teatree.core.gates.review_request_guard import canonical_mr_url, resolve_guard_target, should_post_review_request
from teatree.core.gates.review_request_state_gate import check_reviewed_state, reviewed_state_required
from teatree.core.models import Ticket
from teatree.core.on_behalf_gate_recorded import (
    OnBehalfPostBlockedError,
    on_behalf_block_message,
    require_on_behalf_approval,
)
from teatree.core.on_behalf_post_receipt import notify_user_on_behalf_post
from teatree.core.review_message_cache import persist_review_message
from teatree.loop.review_request_tracker import record_review_request_post
from teatree.types import RawAPIDict

_ACTION = "review_request_post"


# Used when ``--title`` is absent. The command does NOT fetch the live MR
# title (that needs a GitLab token + network — out of scope for "one
# legible recorded-approval post"); ``--title`` is the recommended subject.
_DEFAULT_TITLE = "Please review"


def _iid_from_mr(canonical: str) -> str:
    """Last numeric path segment of the canonical MR URL (the ticket dir key)."""
    for segment in reversed(canonical.split("/")):
        if segment.isdigit():
            return segment
    return canonical.rsplit("/", 1)[-1]


class Command(TyperCommand):
    @command()
    def handle(
        self,
        mr_url: Annotated[str, typer.Option("--mr-url", help="Canonical MR/PR URL to post.")],
        approver: Annotated[str, typer.Option("--approver", help="User id that recorded the #960 approval.")],
        title: Annotated[str, typer.Option("--title", help="Review-request subject (recommended).")] = "",
        ticket_id: Annotated[
            str,
            typer.Option("--ticket-id", help="Ticket pk carrying the #1829 anti-vacuity attestation (gate input)."),
        ] = "",
        head_sha: Annotated[
            str,
            typer.Option("--head-sha", help="Full head SHA the #1829 anti-vacuity attestation must bind to."),
        ] = "",
    ) -> None:
        """Post a review request after #1829 anti-vacuity + #1094 dedup + #960 approval.

        Machine-legible: prints a single JSON dict (``action`` is
        ``post``/``draft``/``suppress``/``refused``) and uses exit codes —
        ``0`` post/draft/suppress, ``2`` refused (no recorded approval / no
        anti-vacuity attestation).
        """
        _ = approver  # the #960 approver is bound at approve-on-behalf record time.

        # #1829 anti-vacuity gate runs FIRST — before the dedup claim or any
        # wire call — so a missing attestation refuses without leaving an
        # orphan ``ReviewRequestPost`` claim to roll back. NO-OP when
        # ``require_anti_vacuity_attestation`` is off (opt-in default).
        anti_vacuity_block = self._anti_vacuity_block(ticket_id, head_sha)
        if anti_vacuity_block:
            self.stdout.write(anti_vacuity_block)
            self._emit(
                {"action": "refused", "reason": "anti_vacuity_not_attested", "mr_url": mr_url},
                exit_code=2,
            )

        # PR-08 review-state gate: refuse a broadcast unless the ticket is
        # REVIEWED with a recorded review-evidence artifact. NO-OP when
        # ``require_reviewed_state_for_review_request`` is off (opt-in default),
        # so a normal reviewed-and-cleared flow is never blocked.
        reviewed_state_block = self._reviewed_state_block(ticket_id)
        if reviewed_state_block:
            self.stdout.write(reviewed_state_block)
            self._emit(
                {"action": "refused", "reason": "ticket_not_reviewed", "mr_url": mr_url},
                exit_code=2,
            )

        target = resolve_guard_target()
        if target is None:
            # The review channel is unpostable (e.g. a Slack Connect channel
            # that requires a user xoxp token the bot doesn't hold — #2231).
            # Fall back to a bot→user DM draft so the user can forward the
            # review request manually. Never silently suppress.
            sent = self._draft_dm_fallback(mr_url=mr_url, title=title)
            self._emit(
                {
                    "action": "draft" if sent else "suppress",
                    "reason": "no_review_channel_or_token",
                    "mr_url": mr_url,
                },
                exit_code=0,
            )

        canonical = canonical_mr_url(mr_url)
        decision = should_post_review_request(mr_url=canonical, target=target)
        if not decision.should_post:
            self._emit(
                {
                    "action": "suppress",
                    "reason": decision.reason,
                    "permalink": decision.permalink,
                    "mr_url": canonical,
                },
                exit_code=0,
            )

        # Peek (non-consuming) so an unapproved post refuses early — before
        # any wire call — and the orphan guard claim is rolled back. The
        # consume happens atomically with the post below (#1879), never here.
        blocked = on_behalf_block_message(canonical, _ACTION)
        if blocked:
            self._rollback_orphan_claim(canonical)
            self.stdout.write(blocked)
            self._emit(
                {"action": "refused", "reason": "on_behalf_not_approved", "mr_url": canonical},
                exit_code=2,
            )

        messaging = messaging_from_overlay()
        if messaging is None:
            self._emit(
                {"action": "suppress", "reason": "no_messaging_backend", "mr_url": canonical},
                exit_code=0,
            )

        text = f"{title or _DEFAULT_TITLE} {canonical}"
        try:
            # consume + post + audit atomic: a failed post rolls back the
            # consume and writes no audit; a BLOCK racing in after the peek
            # raises here and posts nothing.
            resp = require_on_behalf_approval(
                target=canonical,
                action=_ACTION,
                publish=lambda: messaging.post_message(channel=target.channel_id, text=text, thread_ts=""),
            )
        except OnBehalfPostBlockedError as err:
            self._rollback_orphan_claim(canonical)
            self.stdout.write(str(err))
            self._emit(
                {"action": "refused", "reason": "on_behalf_not_approved", "mr_url": canonical},
                exit_code=2,
            )
        ts = str(resp.get("ts", ""))
        permalink = messaging.get_permalink(channel=target.channel_id, ts=ts)

        # Finalize the guard's claim (#1508). ``should_post_review_request``
        # took the ``ReviewRequestPost`` get_or_create claim with an empty
        # ``slack_thread_ts``; without stamping the posted ts here the row
        # keeps the unposted-orphan shape ``_claim_or_reclaim`` reclaims after
        # ``_CLAIM_RACE_WINDOW`` — a later re-attempt would post a duplicate
        # to the review channel (the #1084 incident class).
        record_review_request_post(
            mr_url=canonical,
            slack_channel_id=target.channel_id,
            slack_thread_ts=ts,
        )

        from django.utils import timezone  # noqa: PLC0415

        persist_review_message(
            mr_url=canonical,
            iid=_iid_from_mr(canonical),
            permalink=permalink,
            channel=target.channel_id,
            when=timezone.now(),
        )
        notify_user_on_behalf_post(
            target=canonical,
            action=_ACTION,
            destination=f"review channel {target.channel_id}",
            artifact_url=permalink or canonical,
            summary=text,
        )
        self._emit(
            {"action": "post", "permalink": permalink, "mr_url": canonical},
            exit_code=0,
        )

    @staticmethod
    def _draft_dm_fallback(*, mr_url: str, title: str) -> bool:
        """DM the user a draft review-request when the review channel is unpostable.

        Called when ``resolve_guard_target`` returns ``None`` — the review
        channel is configured but unpostable (e.g. a Slack Connect channel that
        requires a user xoxp token the bot doesn't hold; #2231). A bot→user DM
        gives the user the full text they can forward manually, so the human
        review is never silently lost.

        Returns the ``notify_user`` bool — ``True`` when the DM actually
        landed, ``False`` when no backend / user_id is configured (NOOP). The
        caller emits ``action=draft`` only on ``True``; ``False`` falls back to
        ``action=suppress`` so a genuinely-undeliverable fallback stays loud
        instead of masquerading as a draft.
        """
        from teatree.core.notify import NotifyKind, notify_user  # noqa: PLC0415

        canonical = canonical_mr_url(mr_url)
        subject = title or _DEFAULT_TITLE
        text = (
            f"The review channel is unpostable (no token — Slack Connect channel?). "
            f"Please forward this review request manually:\n\n"
            f"{subject} {canonical}"
        )
        return notify_user(
            text,
            kind=NotifyKind.INFO,
            idempotency_key=f"review_request_draft:{canonical}",
        )

    @staticmethod
    def _anti_vacuity_block(ticket_id: str, head_sha: str) -> str:
        """The #1829 block message, or ``""`` when allowed / the gate is off.

        NO-OP (returns ``""``) when ``require_anti_vacuity_attestation`` is off.
        When on, a ``--ticket-id`` + ``--head-sha`` is required (the gate reads
        the attestation off the ticket and binds it to the head); a missing one
        is itself a block with actionable steering, since the request-review
        transition must be SHA-bound.
        """
        from teatree.core.gates.anti_vacuity_gate import (  # noqa: PLC0415
            AntiVacuityAttestationError,
            anti_vacuity_required,
            check_anti_vacuity_attestation,
        )

        if not anti_vacuity_required():
            return ""
        if not ticket_id.strip() or not head_sha.strip():
            return (
                "request review refused (require_anti_vacuity_attestation): pass --ticket-id and "
                "--head-sha so the anti-vacuity attestation can be verified SHA-bound. Record it first "
                "with `lifecycle record-anti-vacuity <ticket> --head-sha <sha> --ac-coverage <...> "
                "--proven-test <test::id>` (or `--no-new-tests`)."
            )
        try:
            ticket = Ticket.objects.resolve(ticket_id)
        except Ticket.DoesNotExist:
            return f"request review refused: ticket {ticket_id!r} not found (anti-vacuity gate needs a ticket)."
        try:
            check_anti_vacuity_attestation(ticket, head_sha, transition="request review")
        except AntiVacuityAttestationError as exc:
            return str(exc)
        return ""

    @staticmethod
    def _reviewed_state_block(ticket_id: str) -> str:
        """The PR-08 review-state block message, or ``""`` when allowed / off.

        NO-OP (returns ``""``) when ``require_reviewed_state_for_review_request``
        is off. When on, a ``--ticket-id`` is required (the gate reads the
        ticket's FSM state and its review-evidence artifact); a missing or
        unknown ticket is itself a block with actionable steering.
        """
        if not reviewed_state_required():
            return ""
        if not ticket_id.strip():
            return (
                "request review refused (require_reviewed_state_for_review_request): pass --ticket-id so "
                "the gate can verify the ticket is REVIEWED with a recorded review-evidence artifact."
            )
        try:
            ticket = Ticket.objects.resolve(ticket_id)
        except Ticket.DoesNotExist:
            return f"request review refused: ticket {ticket_id!r} not found (review-state gate needs a ticket)."
        return check_reviewed_state(ticket)

    @staticmethod
    def _rollback_orphan_claim(canonical: str) -> None:
        """Delete the guard's just-created ``ReviewRequestPost`` claim on refusal.

        Risk-c: ``should_post_review_request`` already took the atomic
        ``get_or_create`` claim before #960 refused. If a refusal leaves
        that row, every future legitimate post for this MR suppresses with
        ``already_claimed`` forever. Only delete a claim that has no posted
        message yet (``done_at`` unset and no thread ts) — never reconcile
        away a real prior post the guard reconciled.
        """
        from teatree.core.models import ReviewRequestPost  # noqa: PLC0415

        ReviewRequestPost.objects.filter(
            mr_url=canonical,
            done_at__isnull=True,
            slack_thread_ts="",
        ).delete()

    def _emit(self, payload: RawAPIDict, *, exit_code: int) -> NoReturn:
        """Print the single machine-legible JSON dict, then exit.

        Always raises ``SystemExit`` (``0`` post/suppress, ``2`` refused) so
        the handle body has one uniform terminator and no dead ``return``.
        """
        import json  # noqa: PLC0415

        self.stdout.write(json.dumps(payload))
        raise SystemExit(exit_code)
