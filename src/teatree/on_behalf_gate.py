"""On-behalf posting pre-gate — the posture's verdict for one action (#960).

Single source of truth for what teatree does *before* publishing a
colleague-**VISIBLE** post made under
the user's identity to a colleague/customer surface — a PR/MR comment, an
issue comment, a Slack channel/thread message, a Notion post, a PR/MR
approval, or a reaction on someone else's message.

The gate governs colleague-visible posts ONLY. Three carve-outs let the
agent proceed without an approval under a forbidding posture:

*   A *draft*-form action (:data:`_DRAFT_FORM_ACTIONS`, e.g.
    ``post_draft_note``) is the ungated safe-by-default: a draft is never
    visible to colleagues — only the user can submit it — so it needs no
    approval under any posture and resolves to AUTO_DRAFT.
*   An action in the user's ``on_behalf_auto_actions`` allowlist (default
    ``["post_e2e_evidence"]``) resolves straight to PROCEED: it is the
    user's routine self-documentation on their OWN ticket (E2E evidence),
    not a colleague-facing voice, so the user does not have to approve
    their own evidence posts. Clearing the list re-gates those actions.
*   An *author-side* action (:data:`_AUTHOR_SIDE_ACTIONS`, i.e.
    ``reply_to_discussion``) on an MR the OWNER AUTHORED resolves to
    PROCEED. Answering a reviewer on one's own MR is the owner's own
    voice on the owner's own work, not a colleague-facing review post,
    and the owner recorded that it posts autonomously. The carve-out is
    keyed on ``own_mr``, which the CALLER must PROVE from the forge (the
    MR's author versus the configured identity) — it defaults ``False``,
    so every caller that cannot prove it stays gated.

Those carve-outs are the whole purpose of the gate: it keeps the user in
control of their colleague-visible voice while letting the agent draft
freely, self-document on its own work, and answer reviewers on the owner's
own MRs.

The posture that decides is ``Mode.egress``, selected with
``t3 loop preset use <name>``. This module
is intentionally a thin layer depending only on :mod:`teatree.config`
— that lets the resolver be imported from anywhere (including
``teatree.cli`` and ``teatree.core``) without creating circular
dependencies. The orchestration that actually *satisfies* the gate
(recorded-approval consume + audit, auto-draft DM) lives in
:mod:`teatree.core.on_behalf_gate_recorded`, which depends on this
module plus ``teatree.core.models``.

Modes and verdicts
==================

The resolver returns one of three :class:`OnBehalfVerdict` values:

*   :attr:`OnBehalfVerdict.PROCEED` — the post proceeds, no approval needed.
    Returned under a permitting posture for every colleague-visible action.
*   :attr:`OnBehalfVerdict.BLOCK` — no recorded approval matched, the
    caller must NOT publish; it surfaces the blocked post to the user (the
    user-notify path) so the user can record an approval in plain text.
    Returned for every colleague-**visible** action under a forbidding
    posture. A draft-form action NEVER yields BLOCK — it is exempt.
*   :attr:`OnBehalfVerdict.AUTO_DRAFT` — the action is a draft-form post
    (colleague-invisible, revocable) and the caller proceeds autonomously
    while recording a DM to the user with the publish/delete commands.
    Returned for actions in :data:`_DRAFT_FORM_ACTIONS` under BOTH
    postures — a draft is colleague-invisible, so none withholds it.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from teatree.config import get_effective_settings

if TYPE_CHECKING:
    from teatree.config import UserSettings


@dataclass(frozen=True, slots=True)
class OnBehalfContext:
    """What the verdict knows about a post's DESTINATION, beyond its action.

    *overlay* is the overlay the post is addressed to — its ``on_behalf_auto_actions``
    allowlist is the one that governs. The posture itself is box-global, so it is the
    same whichever overlay owns the target.

    *target* is the repo the post addresses, ``""`` when the caller names none.
    It is what separates "no target" (ambient resolution, unchanged) from a NAMED
    target no single overlay owns — an unowned target and a tie between overlays
    alike. That case has no PER-OVERLAY tier of its own to read, so it drops that
    one tier (see :attr:`scope`) instead of inheriting an allowlist entry from an
    overlay with no claim on it; the recorded-approval channel is still its satisfier.

    *own_mr* is the caller's PROVED owner-authorship of the target MR. Every field
    defaults to the value that changes nothing, so an unset one never widens the gate.
    """

    overlay: str | None = None
    own_mr: bool | None = False
    target: str = ""

    @property
    def target_unowned(self) -> bool:
        """A NAMED target that resolved to no single overlay."""
        return bool(self.target) and not self.overlay

    @property
    def scope(self) -> str | None:
        """The overlay whose per-overlay tier the allowlist is read from, if any.

        ``""`` for an unowned NAMED target: the name of no overlay, which
        :func:`~teatree.config.get_effective_settings` resolves as "only the global
        DB scope applies". Dropping THAT ONE TIER is the whole of what "unowned"
        means — an env override the operator set for the session is not an overlay's
        opinion, and silently retiring it is the #3895 failure in miniature,
        permissively as readily as restrictively — and the global scope still applies
        (a workspace default has a claim on every repo).

        ``None`` — the ambient resolution — when no target is named at all.
        """
        return "" if self.target_unowned else self.overlay


class OnBehalfVerdict(StrEnum):
    """The three outcomes :func:`resolve_on_behalf_verdict` returns."""

    PROCEED = "proceed"
    BLOCK = "block"
    AUTO_DRAFT = "auto_draft"


# Actions that publish in a colleague-INVISIBLE, revocable draft form.
# These are EXEMPT from the on-behalf gate under *every* posture: a draft is
# never visible to colleagues (only the user can submit it), so it needs
# no approval — that is the whole point of the gate, which exists to keep
# the user in control of their colleague-VISIBLE voice. A draft-form
# action therefore never BLOCKs; it resolves to AUTO_DRAFT (post the draft
# autonomously + DM the user the publish/delete commands). Every action
# NOT in this set is a colleague-visible post and stays gated exactly as
# before. This set is the single source of truth for the draft carve-out.
_DRAFT_FORM_ACTIONS: frozenset[str] = frozenset({"post_draft_note"})


# Actions that are the MR AUTHOR answering a reviewer on their own thread.
# Exempt from the gate ONLY when the caller has PROVED the owner authored
# the MR (``own_mr=True``) — the owner recorded (2026-07-23) that
# author-side replies on their own MRs post autonomously, with no
# draft-first and no per-reply approval. The two conditions are
# independent locks: an action outside this set stays gated even with
# ``own_mr=True`` (an approve/unapprove/live comment on one's own MR is
# NOT covered), and an action inside it stays gated whenever authorship
# is unproven. The receipt DM (``notify_on_post_on_behalf``) is untouched
# — autonomy here is the absence of a pre-ask, never of visibility.
_AUTHOR_SIDE_ACTIONS: frozenset[str] = frozenset({"reply_to_discussion"})


# The agent-driven review-request post action (mirrors ``_ACTION`` in
# ``teatree.core.management.commands.review_request_post``). When the resolved
# ``review_request_post_disabled`` is true (the ``notify`` tier sets it, or the
# user pinned it), this one action BLOCKs regardless of the posture —
# the customer-overlay done-definition gate.
_REVIEW_REQUEST_POST_ACTION: str = "review_request_post"

_OWNER_AUTHORSHIP_REQUIRED_ACTIONS: frozenset[str] = frozenset(
    {"review_nag_post", "review_request_resume_post", _REVIEW_REQUEST_POST_ACTION}
)


def on_behalf_authorship_refusal(action: str, context: OnBehalfContext | None) -> bool:
    """Whether *action* intrinsically refuses without proved owner authorship."""
    effective = context or OnBehalfContext()
    return action in _OWNER_AUTHORSHIP_REQUIRED_ACTIONS and effective.own_mr is not True


def resolve_on_behalf_verdict(
    action: str, context: OnBehalfContext | None = None, *, egress_forbidden: bool
) -> OnBehalfVerdict:
    """Return the verdict for *action* under the active posture.

    *egress_forbidden* is the active posture's opinion on acting outward at all (B6's AFK
    line, ``Mode.egress``). It is passed IN rather than resolved here because this module
    is the config-only leaf every layer may import; the ORM-side resolution lives in
    :func:`teatree.core.on_behalf_gate_recorded.resolve_posture_verdict`, which is what
    every real caller asks. True BLOCKs every colleague-visible action, leaving the three
    carve-outs below intact: a draft is colleague-invisible, an author-side reply on the owner's own
    MR is the owner's own work, and ``on_behalf_auto_actions`` is a standing allowlist the
    owner wrote. It carries no default: an omitted posture would answer PROCEED for a caller
    that never asked the posture at all.

    The gate covers colleague-**VISIBLE** posts only. Three carve-outs proceed
    without an approval even under a forbidding posture:

    *   an action in the resolved ``on_behalf_auto_actions`` allowlist
        (default ``["post_e2e_evidence"]``) → :attr:`OnBehalfVerdict.PROCEED`
        under every posture (the user's own self-documentation, never a
        colleague-facing voice).
    *   a draft-form action (one of :data:`_DRAFT_FORM_ACTIONS`) is
        colleague-invisible and revocable, so it is exempt under every posture
        and never BLOCKs → :attr:`OnBehalfVerdict.AUTO_DRAFT`: post the draft
        autonomously and DM the user the publish/delete commands.
    *   an author-side action (one of :data:`_AUTHOR_SIDE_ACTIONS`) with
        ``context.own_mr`` PROVED true → :attr:`OnBehalfVerdict.PROCEED`: the owner
        answering a reviewer on the owner's own MR. *own_mr* is a fact only
        the caller can establish (a forge read of the MR's author against the
        configured identity) and defaults ``False``, so an unproven or
        unprovable authorship keeps the post gated. It is inert for every
        action outside the set — passing ``own_mr=True`` for ``approve``
        still BLOCKs.

    For every other colleague-visible action:

    *   a permitting posture → :attr:`OnBehalfVerdict.PROCEED`.
    *   a forbidding posture → :attr:`OnBehalfVerdict.BLOCK`.

    The posture is box-global, so it needs no per-overlay resolution. What still
    resolves per-overlay is the carve-out data this function reads — chiefly
    ``on_behalf_auto_actions`` — through the standard env → target-overlay → global
    → default chain via :func:`teatree.config.get_effective_settings`. The env layer
    applies on BOTH branches (``apply_env=True``): a named overlay otherwise drops
    it, silently retiring an override that must never stop applying.

    *context* carries what the caller knows about the post's destination (see
    :class:`OnBehalfContext`) — chiefly WHICH overlay's allowlist governs, via
    :attr:`OnBehalfContext.scope`. A NAMED target no single overlay owns drops the
    PER-OVERLAY tier only, landing on env → global → the shipped default: a target
    no overlay enumerates is the COMMON case on a single-overlay install, so
    dropping more than that one tier would retire an operator's override almost
    everywhere.

    One posture-independent override sits above the table: when the resolved
    ``review_request_post_disabled`` is true, the single action
    ``review_request_post`` BLOCKs regardless of the posture — even a permitting one.
    No autonomy tier reaches the posture (#3895): opening colleague egress is its own
    named opt-in, so an autonomous overlay still BLOCKs here before the flag is read.
    The autonomy TIER drives the flag (#2579): the ``notify`` tier resolves it true
    (a collaborative/customer surface keeps a human in the merge loop and stops at
    "MR is mergeable + review-requestable", never auto-requesting review), while
    the ``full`` tier resolves it false (a solo tooling surface auto-requests). An
    explicit per-overlay pin always wins. It is scoped to that one action — every
    other colleague-visible post resolves through the table below unchanged.
    """
    context = context or OnBehalfContext()
    settings = get_effective_settings(context.scope, apply_env=True)
    carved = _carve_out_verdict(action, context, settings)
    if carved is not None:
        return carved
    return OnBehalfVerdict.BLOCK if egress_forbidden else OnBehalfVerdict.PROCEED


def _carve_out_verdict(action: str, context: OnBehalfContext, settings: "UserSettings") -> OnBehalfVerdict | None:
    """The verdict for an action the posture table does not decide, else ``None``.

    Each arm is independent of the active posture, because each is about WHAT the action
    is rather than how loudly the owner wants to be asked.
    """
    if on_behalf_authorship_refusal(action, context):
        return OnBehalfVerdict.BLOCK
    # Posture-independent override: review-request posting is BLOCKed when the
    # resolved ``review_request_post_disabled`` is true (the ``notify`` tier sets
    # it, or the user pinned it), so this one action BLOCKs even under a permitting
    # posture. Scoped to ``review_request_post`` — it never collapses any other action.
    if action == _REVIEW_REQUEST_POST_ACTION and settings.review_request_post_disabled:
        return OnBehalfVerdict.BLOCK
    # Auto-proceed actions are the user's routine self-documentation on their
    # OWN ticket (E2E evidence) — not a colleague-facing voice — so they need
    # no per-post approval and proceed directly under a forbidding posture.
    if action in settings.on_behalf_auto_actions:
        return OnBehalfVerdict.PROCEED
    # The owner answering a reviewer on the owner's OWN MR — their own voice
    # on their own work, exempted by owner decision (2026-07-23). Both
    # conditions must hold, so neither half can widen the carve-out alone.
    if context.own_mr and action in _AUTHOR_SIDE_ACTIONS:
        return OnBehalfVerdict.PROCEED
    # Colleague-invisible, so no posture refuses it — but AUTO_DRAFT is what earns the
    # owner the DM naming the publish/delete commands, which PROCEED would drop.
    if action in _DRAFT_FORM_ACTIONS:
        return OnBehalfVerdict.AUTO_DRAFT
    return None


def on_behalf_post_will_block(action: str, context: OnBehalfContext | None = None, *, egress_forbidden: bool) -> bool:
    """Whether *action* WILL BLOCK under the given posture — the proactive pre-check.

    The forward-looking companion to :func:`resolve_on_behalf_verdict`: a caller
    runs this BEFORE attempting a colleague-visible on-behalf post so it can
    surface the owner's solution-oriented choice up front — select a permitting
    posture durably, or approve just this once (see
    :func:`teatree.core.on_behalf_gate_recorded.format_on_behalf_block_message`) —
    instead of blundering into the BLOCK and only then reacting. The gate is an
    extra safety net, not the primary control (``/t3:rules`` § "Anticipate a
    Predictable Gate"), so anticipating the predictable block one action ahead is
    the point: teatree should ideally never hit it.

    ``True`` iff the verdict is :attr:`OnBehalfVerdict.BLOCK`; a draft-form action
    (AUTO_DRAFT) and an :attr:`OnBehalfVerdict.PROCEED` action both return
    ``False`` — neither needs a pre-ask. *context* must be the one the publish
    will pass, or the pre-check and the verdict disagree about the same post.
    """
    return resolve_on_behalf_verdict(action, context, egress_forbidden=egress_forbidden) is OnBehalfVerdict.BLOCK
