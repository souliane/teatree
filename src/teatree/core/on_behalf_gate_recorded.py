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
on the tri-state :class:`~teatree.config.OnBehalfPostMode`:

*   :attr:`~teatree.on_behalf_gate.OnBehalfVerdict.PROCEED` (mode
    :attr:`~teatree.config.OnBehalfPostMode.IMMEDIATE`) → return, the post
    proceeds;
*   :attr:`~teatree.on_behalf_gate.OnBehalfVerdict.AUTO_DRAFT`
    (action is a colleague-invisible draft-form post like
    ``post_draft_note`` under either
    :attr:`~teatree.config.OnBehalfPostMode.ASK` or
    :attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK` — drafts are
    exempt from the gate under every blocking mode) → emit a
    fire-and-forget bot→user DM and return; the post proceeds without
    consuming any recorded approval. The audit lives on the ``BotPing``
    ledger (``notify_user``); no ``OnBehalfAudit`` row is written because
    no approval was needed;
*   :attr:`~teatree.on_behalf_gate.OnBehalfVerdict.BLOCK`
    (a colleague-VISIBLE action under
    :attr:`~teatree.config.OnBehalfPostMode.ASK` or
    :attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK`) + a recorded,
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

Drafts are the ungated safe-by-default: every mode publishes draft-form
notes autonomously (drafts are colleague-invisible and revocable, so they
need no approval) while ASK / DRAFT_OR_ASK block every colleague-VISIBLE
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

from collections.abc import Callable

from teatree.on_behalf_gate import OnBehalfVerdict, resolve_on_behalf_verdict


def format_on_behalf_block_message(target: str, action: str) -> str:
    """The exact on-behalf BLOCK message (``target``/``action`` interpolated).

    Pure, ORM-free, side-effect-free — the SINGLE SOURCE OF TRUTH for both
    :class:`OnBehalfPostBlockedError`'s message and the eval harness's gate-aware
    ``t3@on_behalf_ask`` CLI stub, so the stub's refusal text can never drift from
    the production block message (vendored-by-derivation + a parity test, per
    ``/t3:rules`` § "Read the Canonical Source Before Fixing a Conformance Bug").
    """
    return (
        f"on-behalf post blocked by on_behalf_post_mode (#960): "
        f"{action} on {target!r} needs explicit user approval first. "
        f"The user records it (no terminal required) with:\n"
        f"    t3 review approve-on-behalf {target!r} {action} --approver <user-id>\n"
        f"then the agent re-runs this post. Never publish unattended."
    )


class OnBehalfPostBlockedError(RuntimeError):
    """BLOCK verdict and no recorded approval — the on-behalf post must NOT publish.

    Carries ``target``/``action`` plus a user-facing message that names the
    exact ``t3 review approve-on-behalf`` invocation that satisfies the
    gate, so the blocked post can be surfaced to the user verbatim.
    """

    def __init__(self, target: str, action: str) -> None:
        self.target = target
        self.action = action
        super().__init__(format_on_behalf_block_message(target, action))


def require_on_behalf_approval[PublishResult](
    *,
    target: str,
    action: str,
    publish: Callable[[], PublishResult],
    taint: str | None = None,
) -> PublishResult:
    """Gate one on-behalf post against the tri-state mode and run it atomically.

    See the module docstring for the four-outcome table. ``publish`` performs
    the colleague-visible side-effect and returns its result (the posted
    artifact ref). Fail-closed: an unresolved (default) setting maps to
    :attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK`. Under both
    blocking modes (ASK and DRAFT_OR_ASK) a colleague-VISIBLE action — any
    action NOT in :data:`~teatree.on_behalf_gate._DRAFT_FORM_ACTIONS` —
    BLOCKs when no recorded approval matches; a draft-form action is exempt
    and AUTO_DRAFTs.

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
    if taint is None:
        taint = _default_owner_taint()
    verdict = resolve_on_behalf_verdict(action)
    if verdict is OnBehalfVerdict.PROCEED:
        return publish()
    if verdict is OnBehalfVerdict.AUTO_DRAFT:
        _notify_on_behalf_autodraft(target=target, action=action)
        return publish()

    from django.db import transaction  # noqa: PLC0415

    from teatree.core.models.on_behalf_approval import OnBehalfApproval, OnBehalfAudit  # noqa: PLC0415

    with transaction.atomic():
        consumed = OnBehalfApproval.consume(target, action)
        if consumed is None and _policy_grants_on_behalf(taint):
            OnBehalfApproval.record(target, action, _POLICY_APPROVER)
            consumed = OnBehalfApproval.consume(target, action)
        if consumed is None:
            raise OnBehalfPostBlockedError(target, action)
        result = publish()
        OnBehalfAudit.objects.create(
            approval=consumed,
            target=consumed.target,
            action=consumed.action,
            approver_id=consumed.approver_id,
        )
        return result


def on_behalf_block_message(target: str, action: str, *, taint: str | None = None) -> str:
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
    :class:`OnBehalfPostBlockedError` message.
    """
    if taint is None:
        taint = _default_owner_taint()
    verdict = resolve_on_behalf_verdict(action)
    if verdict is not OnBehalfVerdict.BLOCK:
        return ""

    from teatree.core.models.on_behalf_approval import OnBehalfApproval  # noqa: PLC0415

    if OnBehalfApproval.has_unconsumed(target, action) or _policy_grants_on_behalf(taint):
        return ""
    return str(OnBehalfPostBlockedError(target, action))


#: The approver id an on-behalf post graduated by the #119 dial is recorded under —
#: a non-agent authority (``is_non_reviewer_role`` passes it), so the audit names the
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
    from teatree.core.models.approval_dial import policy_dial  # noqa: PLC0415
    from teatree.core.models.approval_policy import ON_BEHALF_POST, Decision, approval_policy  # noqa: PLC0415

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
    from teatree.core.notify import NotifyKind, notify_user  # noqa: PLC0415

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
    )
