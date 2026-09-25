"""Recorded-approval orchestration for the on-behalf pre-gate (#960/#961).

``teatree.on_behalf_gate`` holds the pure setting resolver
(``resolve_on_behalf_verdict``) — it depends only on
``teatree.config`` and stays in that thin layer. The *satisfiable*
channel needs the :class:`~teatree.core.models.on_behalf_approval.OnBehalfApproval`
/ :class:`~teatree.core.models.on_behalf_approval.OnBehalfAudit` ORM models,
so its orchestration lives here in ``teatree.core`` (which legitimately
depends on both ``teatree.on_behalf_gate`` and ``teatree.core.models``),
exactly as #953 split ``teatree.utils.approval`` (pure) from
``teatree.core.gates.db_approval_gate`` (ORM-backed).

:func:`require_on_behalf_approval` is the single chokepoint helper every
on-behalf publish path calls *before* it publishes. Its outcome depends
on the active posture:

*   :attr:`~teatree.on_behalf_gate.OnBehalfVerdict.PROCEED` (a permitting
    posture) → return, the post proceeds;
*   :attr:`~teatree.on_behalf_gate.OnBehalfVerdict.AUTO_DRAFT`
    (action is a colleague-invisible draft-form post like
    ``post_draft_note`` — drafts are exempt under BOTH postures) → emit a
    fire-and-forget bot→user DM and return; the post proceeds without
    consuming any recorded approval. The audit lives on the ``BotPing``
    ledger (``notify_user``); no ``OnBehalfAudit`` row is written because
    no approval was needed;
*   :attr:`~teatree.on_behalf_gate.OnBehalfVerdict.BLOCK`
    (a colleague-VISIBLE action under a forbidding posture) + a recorded,
    unconsumed, exactly-scoped
    :class:`OnBehalfApproval` → inside ONE ``transaction.atomic`` block:
    consume it single-use, run the caller's ``publish`` side-effect, write
    an :class:`OnBehalfAudit` row — all-or-nothing. The post's result is
    returned;
*   BLOCK + no recorded approval → raise :class:`OnBehalfPostBlockedError`
    *before* ``publish`` runs. The caller never publishes; it surfaces the
    blocked post to the user (the user-notify path) so the user can approve
    it in plain text by recording an approval — never a silent drop, never
    an unattended post.

The post is supplied as a ``publish`` callback so consume, post and audit
share one transaction (#1879). Previously the gate consumed the single-use
approval and wrote the audit in a transaction *separate* from the caller's
later post: a post that failed after the gate returned burned the approval
(forcing the user to re-approve) and left an :class:`OnBehalfAudit` row
claiming a post that never happened. A ``publish`` that raises now rolls the
whole block back — the approval is NOT burned, no audit is written, and a
retry can reuse the same recorded approval. This makes the
post→succeed→consume+audit invariant structural, the same way
:meth:`DeferredQuestion.consume` / ``MergeClear`` / ``DbApproval`` co-locate
consume and audit in one block, and ``red_card`` / ``review_request_merge_react``
use the post→verify→stamp order for reactions.

:func:`on_behalf_block_message` is the *non-consuming* peek: it returns the
blocked-post message (or ``""`` when the post may proceed) without consuming
any approval or running any side-effect — for callers that surface an early
refusal before doing expensive prep, then publish through
:func:`require_on_behalf_approval`. The consuming path is exactly
:func:`require_on_behalf_approval`; the peek can never burn an approval.

Drafts are the ungated safe-by-default: every posture publishes draft-form
notes autonomously (drafts are colleague-invisible and revocable, so they
need no approval) while a forbidding posture blocks every colleague-VISIBLE
mutation until the user records an approval. The user satisfies the gate
for a visible post **without a TTY** via ``t3 review approve-on-behalf
<target> <action> --approver <id>`` (the #777/#953 interactive-TTY-only
anti-pattern is deliberately avoided).

The ORM-model imports (``OnBehalfApproval`` / ``OnBehalfAudit``) live
inside the functions rather than at module top because
``teatree.cli.review.on_behalf`` imports this module lazily so the
``teatree.cli`` package can be loaded before ``django.setup()`` runs (typer
command discovery, ``--help`` rendering, the privacy-scan subprocess). An
eager ORM import here would defeat the lazy chain and crash the CLI with
``ImproperlyConfigured`` (see souliane/teatree#1003).
"""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from teatree.core.mode_resolution import owner_voice_forbidden, resolve_active_mode
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.on_behalf_gate import (
    OnBehalfContext,
    OnBehalfVerdict,
    on_behalf_authorship_refusal,
    resolve_on_behalf_verdict,
)

if TYPE_CHECKING:
    from teatree.core.models.on_behalf_approval import OnBehalfApproval

logger = logging.getLogger(__name__)


def resolve_posture_verdict(action: str, context: OnBehalfContext | None = None) -> OnBehalfVerdict:
    """The on-behalf verdict WITH the active posture's egress opinion applied.

    The one verdict every real caller asks. :func:`~teatree.on_behalf_gate.resolve_on_behalf_verdict`
    is the pure settings half and cannot read the ``Mode`` row without inverting the
    dependency that keeps it importable from the CLI and the hooks.

    The preset table decides what RUNS; this gate decides what SPEAKS. A refusal here is the
    posture's ordinary per-action AFK line, so it is quiet.
    """
    return resolve_on_behalf_verdict(action, context, egress_forbidden=owner_voice_forbidden())


def owner_voice_permitted() -> bool:
    """Whether the active posture lets the owner's voice out — the owner's global opt-in.

    The one thing a PROCEED verdict does not tell its caller: PROCEED is also reached
    through the ``on_behalf_auto_actions`` allowlist and the author-side own-MR reply,
    and only the posture is the global statement the #1207 live-post token defers to.
    Reads the same fail-closed seam as :func:`resolve_posture_verdict`, so the two can
    never disagree about the same post.
    """
    return not owner_voice_forbidden()


def posture_refusal_cause() -> str:
    """Which posture refuses the owner's voice, and the layer that selected it (a manual override, a schedule slot)."""
    active = resolve_active_mode()
    return f"the active posture {active.name!r} forbids acting on the owner's behalf ({active.reason})"


def format_on_behalf_block_message(target: str, action: str) -> str:
    """The exact on-behalf BLOCK message (``target``/``action`` interpolated).

    Pure, ORM-free, side-effect-free — the SINGLE SOURCE OF TRUTH for both
    :class:`OnBehalfPostBlockedError`'s message and the eval harness's gate-aware
    ``t3@on_behalf_forbidden`` CLI stub, so the stub's refusal text can never drift from
    the production block message (vendored-by-derivation + a parity test, per
    ``/t3:rules`` § "Read the Canonical Source Before Fixing a Conformance Bug").

    The message names BOTH **solution-oriented** ways to clear the gate — select a
    permitting posture durably, or approve just this once — and never the wrong
    "bypass the gate or do it yourself" pair (``/t3:rules`` § "Anticipate a
    Predictable Gate"). Best used *proactively*: a caller that can foresee the
    block via :func:`teatree.on_behalf_gate.on_behalf_post_will_block` surfaces
    this choice to the owner BEFORE attempting the post, so the reactive raise is
    rarely reached.
    """
    return (
        f"on-behalf post blocked by the active posture: "
        f"{action} on {target!r} needs explicit user approval first. "
        f"Offer the owner the solution-oriented choice — never bypass-or-DIY:\n"
        f"  1. Select a posture that permits it durably: "
        f"t3 loop preset use present --reason <why>\n"
        f"  2. Approve just this once (no terminal required): "
        f"t3 review approve-on-behalf {target!r} {action} --approver <user-id>\n"
        f"then the agent re-runs this post. Never publish unattended."
    )


class OnBehalfPartialPublishError(RuntimeError):
    """A publish that FAILED after some of its colleague-visible posts already landed.

    Forge posts are not transactional, so a batch that fails on its third comment
    leaves the first two permanently readable under the user's identity. Both halves
    of the enclosing block would otherwise be undone together, and only one of them
    should be: the approval consume rolls back (the authorization did not fully
    deliver, so it survives the retry) while the audit for what DID publish must
    persist — an on-behalf post a colleague can read with no audit row is exactly
    what the audit exists to make impossible.

    Carries the body's own ``(message, code)`` so
    :func:`~teatree.cli.review.on_behalf._surface` re-emits it verbatim once the
    rollback has happened.
    """

    def __init__(self, result: tuple[str, int]) -> None:
        super().__init__(result[0])
        self.result = result


def format_posture_block_message(target: str, action: str, cause: str) -> str:
    """The BLOCK message naming the posture that refused and the layer that selected it."""
    return (
        f"on-behalf post blocked by the active posture: "
        f"{action} on {target!r} needs explicit user approval first — {cause}. "
        f"Offer the owner the solution-oriented choice — never bypass-or-DIY:\n"
        f"  1. Select a posture that permits it durably: "
        f"t3 loop preset use present --reason <why> "
        f"(t3 loop preset auto lifts a manual override; t3 loop preset show names the layer in force)\n"
        f"  2. Approve just this once (no terminal required): "
        f"t3 review approve-on-behalf {target!r} {action} --approver <user-id>\n"
        f"then the agent re-runs this post. Never publish unattended."
    )


class OnBehalfPostBlockedError(RuntimeError):
    """BLOCK verdict and no recorded approval — the on-behalf post must NOT publish.

    Carries ``target``/``action`` plus a user-facing message that names the
    exact ``t3 review approve-on-behalf`` invocation that satisfies the
    gate, so the blocked post can be surfaced to the user verbatim. *cause* is
    :func:`posture_refusal_cause`'s answer, naming the posture and the layer that
    selected it; empty keeps the generic message.
    """

    def __init__(self, target: str, action: str, cause: str = "") -> None:
        self.target = target
        self.action = action
        self.cause = cause
        super().__init__(
            format_posture_block_message(target, action, cause)
            if cause
            else format_on_behalf_block_message(target, action)
        )


class OnBehalfAuthorshipRefusedError(OnBehalfPostBlockedError):
    """An intrinsic refusal that no approval, dial, mode, or allowlist can override."""

    def __init__(self, target: str, action: str) -> None:
        self.target = target
        self.action = action
        RuntimeError.__init__(self, format_on_behalf_authorship_refusal(target, action))


def format_on_behalf_authorship_refusal(target: str, action: str) -> str:
    """User-facing refusal without misleading approval instructions."""
    return (
        f"on-behalf post intrinsically refused: {action} on {target!r} requires "
        "fresh forge proof that the owner authored the merge request; authorship is unproved."
    )


def _require_owner_authorship(*, target: str, action: str, context: OnBehalfContext | None) -> None:
    if on_behalf_authorship_refusal(action, context):
        raise OnBehalfAuthorshipRefusedError(target, action)


def require_on_behalf_approval[PublishResult](
    *,
    target: str,
    action: str,
    publish: Callable[[], PublishResult],
    taint: str | None = None,
    context: OnBehalfContext | None = None,
) -> PublishResult:
    """Gate one on-behalf post against the active posture and run it atomically.

    See the module docstring for the four-outcome table. ``publish`` performs
    the colleague-visible side-effect and returns its result (the posted
    artifact ref). Fail-closed: a posture that cannot be READ forbids the owner's
    voice. Under a forbidding posture a colleague-VISIBLE action — any action
    NOT in :data:`~teatree.on_behalf_gate._DRAFT_FORM_ACTIONS` — BLOCKs when no
    recorded approval matches; a draft-form action is exempt and AUTO_DRAFTs.
    *context* is what the caller knows about the post's destination
    (:class:`~teatree.on_behalf_gate.OnBehalfContext`): the overlay whose allowlist
    governs, and the PROVED owner-authorship that exempts an author-side reply.
    Omitted, both resolve as they did before it existed.

    *   PROCEED / AUTO_DRAFT → run ``publish`` and return its result (no
        consume, no audit; AUTO_DRAFT also emits the autodraft DM first).
    *   BLOCK + recorded approval → inside one ``transaction.atomic`` block
        consume the approval, run ``publish``, write the audit, return the
        result. A ``publish`` that raises rolls back the consume and the
        audit (#1879) — the approval survives for a retry, no audit lies.
    *   BLOCK + no recorded approval, but the #119 dial GRADUATED the
        ``on_behalf_post`` class for an owner-taint post — record a single-use
        ``policy`` approval, consume it, publish, and audit exactly as the
        recorded-approval path. ``taint`` is the content's provenance
        (default OWNER, matching ``SendRequest.provenance``); a caller relaying
        untrusted content passes ``Provenance.PUBLIC`` to invoke the floor.
    *   BLOCK + no approval + no graduation → raise
        :class:`OnBehalfPostBlockedError` before ``publish`` runs.
    """
    _require_owner_authorship(target=target, action=action, context=context)
    if taint is None:
        taint = _default_owner_taint()
    verdict = resolve_posture_verdict(action, context)
    if verdict is OnBehalfVerdict.PROCEED:
        return publish()
    if verdict is OnBehalfVerdict.AUTO_DRAFT:
        _notify_on_behalf_autodraft(target=target, action=action)
        return publish()

    from django.db import transaction  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.core.models.on_behalf_approval import (  # noqa: PLC0415 — deferred: the ORM model package eager-loads every model, so an import-scope import drags the app registry in pre-``django.setup()`` and crashes the CLI bootstrap (souliane/teatree#1003)
        OnBehalfApproval,
        OnBehalfAudit,
    )

    spent: OnBehalfApproval | None = None
    try:
        with transaction.atomic():
            consumed = OnBehalfApproval.consume(target, action)
            if consumed is None and _policy_grants_on_behalf(taint):
                OnBehalfApproval.record(target, action, _POLICY_APPROVER)
                consumed = OnBehalfApproval.consume(target, action)
            if consumed is None:
                raise OnBehalfPostBlockedError(target, action, posture_refusal_cause())
            spent = consumed
            result = publish()
            OnBehalfAudit.objects.create(
                approval=consumed,
                target=consumed.target,
                action=consumed.action,
                approver_id=consumed.approver_id,
            )
            return result
    except OnBehalfPartialPublishError as partial:
        # The block has already rolled back here, so the consume is undone and the
        # approval survives for the retry — right, since it did not fully deliver.
        # What did deliver is colleague-visible for good, so it is audited outside
        # the rollback rather than erased with it.
        if spent is not None:
            # An audit failure must not REPLACE the partial-publish signal: the caller's
            # `_surface` maps four exception types, so any other one reaches it as an
            # unmapped crash — losing both the report of what landed and the audit.
            try:
                _audit_the_posts_that_landed(spent)
            except Exception:
                logger.exception("on-behalf: could not audit the posts that landed for %s/%s", target, action)
        cause = partial.__cause__
        # `_surface` maps this error to `(message, 1)`, so a wrapped interrupt would vanish
        # into a report; `from None` because `cause` already IS this error's own cause.
        if cause is not None and not isinstance(cause, Exception):
            raise cause from None
        raise


def _audit_the_posts_that_landed(spent: "OnBehalfApproval") -> None:
    """Audit a partial publish AFTER its consume rolled back, in a transaction of its own.

    *spent* is the in-memory row the rolled-back block consumed. Its DB row normally
    survives (only the ``consumed_at`` stamp was undone), so the audit points at it as
    usual. The one case it does not is the #119 policy graduation, which RECORDED its
    approval inside the block that just rolled back — the audit's subject went with it,
    so it is re-recorded, spent, since the posts did land under it and the retry
    graduates a fresh grant of its own.
    """
    from django.db import transaction  # noqa: PLC0415 — deferred: Django import at call time
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.core.models.on_behalf_approval import (  # noqa: PLC0415 — deferred: the ORM model package eager-loads every model, so an import-scope import drags the app registry in pre-``django.setup()`` and crashes the CLI bootstrap (souliane/teatree#1003)
        OnBehalfApproval,
        OnBehalfAudit,
    )

    with transaction.atomic():
        approval = OnBehalfApproval.objects.filter(pk=spent.pk).first()
        if approval is None:
            approval = OnBehalfApproval.record(spent.target, spent.action, spent.approver_id)
            approval.consumed_at = timezone.now()
            approval.save(update_fields=["consumed_at"])
        OnBehalfAudit.objects.create(
            approval=approval,
            target=approval.target,
            action=approval.action,
            approver_id=approval.approver_id,
        )


def on_behalf_block_message(
    target: str, action: str, *, taint: str | None = None, context: OnBehalfContext | None = None
) -> str:
    """Return the blocked-post message, or ``""`` when the post may proceed.

    The *non-consuming* peek: it never consumes an approval, writes an audit,
    or runs a side-effect — it only reports whether
    :func:`require_on_behalf_approval` would raise for this (target, action).
    Callers that do expensive prep before publishing use it to refuse early;
    the real publish then goes through :func:`require_on_behalf_approval`,
    which consumes the approval atomically with the post.

    PROCEED / AUTO_DRAFT → ``""`` (the post may proceed; the autodraft DM is
    deferred to the atomic publish so a peek never DMs). BLOCK + an
    unconsumed matching approval → ``""``. BLOCK + the #119 dial graduated the
    class for this *taint* → ``""`` (the real publish grants it by policy).
    BLOCK + no approval + no graduation → the actionable
    :class:`OnBehalfPostBlockedError` message. *context* must be the one the
    publish will pass, or the peek and the publish disagree about the same post.
    """
    if on_behalf_authorship_refusal(action, context):
        return format_on_behalf_authorship_refusal(target, action)
    if taint is None:
        taint = _default_owner_taint()
    verdict = resolve_posture_verdict(action, context)
    if verdict is not OnBehalfVerdict.BLOCK:
        return ""

    from teatree.core.models.on_behalf_approval import OnBehalfApproval  # noqa: PLC0415 — deferred: ORM/app-registry

    if OnBehalfApproval.has_unconsumed(target, action) or _policy_grants_on_behalf(taint):
        return ""
    return str(OnBehalfPostBlockedError(target, action, posture_refusal_cause()))


#: The approver id an on-behalf post graduated by the #119 dial is recorded under —
#: a non-agent authority (``is_independent_reviewer_identity`` admits it), so the audit names the
#: standing operator dial config, not the executing agent self-authorizing.
_POLICY_APPROVER = "policy"


def _default_owner_taint() -> str:
    """The default on-behalf ``taint`` — ``Provenance.OWNER`` (the operator's own post).

    Deferred like :func:`teatree.core.send_proxy._default_provenance`: the
    ``Provenance`` enum lives in the ORM model package whose ``__init__`` eager-
    loads every model, so importing it at module scope would drag the registry in
    pre-``django.setup()`` and crash the CLI bootstrap (souliane/teatree#1003).
    """
    from teatree.core.models.provenance import Provenance  # noqa: PLC0415 — deferred: ORM model pkg, pre-app-registry

    return Provenance.OWNER.value


def _policy_grants_on_behalf(taint: str) -> bool:
    """True iff the #119 dial AUTO-approves an ``on_behalf_post`` at content *taint*.

    Fail-closed: any error resolving the dial (or an untrusted *taint* hitting the
    floor) returns ``False`` — the gate then BLOCKs exactly as before.
    """
    from teatree.core.models.approval_dial import policy_dial  # noqa: PLC0415 — deferred: ORM/app-registry
    from teatree.core.models.approval_policy import (  # noqa: PLC0415 — deferred: ORM import needs the app registry
        ON_BEHALF_POST,
        Decision,
        approval_policy,
    )

    try:
        return approval_policy(ON_BEHALF_POST, taint, dial=policy_dial) is Decision.AUTO_APPROVE
    except Exception:  # noqa: BLE001 — an unreadable dial fails CLOSED to BLOCK.
        return False


def _notify_on_behalf_autodraft(*, target: str, action: str) -> None:
    """Fire-and-forget DM the user when a draft-form post auto-publishes.

    Idempotency key ``on_behalf_autodraft:{target}:{action}`` guarantees
    one DM per (target, action) pair across retries within the
    ``BotPing`` ledger window — a second auto-publish of the same draft
    note is a no-op on the notification side (the GitLab API call still
    runs; only the DM is dedup'd).

    Never raises into the caller: ``notify_user`` already wraps every
    transport failure into a NOOP/FAILED ``BotPing`` row and returns
    ``False``. A misconfigured Slack backend must never block a
    legitimate autonomous draft-note publish.
    """
    from teatree.core.notify import NotifyKind, notify_user  # noqa: PLC0415 — deferred: call-time import, kept lazy

    text = (
        f"Posted a draft note autonomously under your identity ({action} on `{target}`). "
        f"Drafts are not visible to colleagues until published.\n\n"
        f"Publish:   `t3 review publish-draft-notes <repo> <mr>`\n"
        f"Discard:   `t3 review delete-draft-note <repo> <mr> <note_id>` "
        f"(see `t3 review list-draft-notes <repo> <mr>` for the id)."
    )
    notify_user(
        text,
        kind=NotifyKind.INFO,
        idempotency_key=f"on_behalf_autodraft:{target}:{action}",
        audience=NotifyAudience.COLLEAGUE_ACTION,
    )
