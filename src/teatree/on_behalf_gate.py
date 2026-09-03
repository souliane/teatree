"""On-behalf posting pre-gate — tri-state resolver (#960).

Single source of truth for the *tri-state* setting that decides what
teatree does *before* publishing a colleague-**VISIBLE** post made under
the user's identity to a colleague/customer surface — a PR/MR comment, an
issue comment, a Slack channel/thread message, a Notion post, a PR/MR
approval, or a reaction on someone else's message.

The gate governs colleague-visible posts ONLY. Three carve-outs let the
agent proceed without an approval under the blocking modes:

*   A *draft*-form action (:data:`_DRAFT_FORM_ACTIONS`, e.g.
    ``post_draft_note``) is the ungated safe-by-default: a draft is never
    visible to colleagues — only the user can submit it — so it needs no
    approval under any mode and resolves to AUTO_DRAFT.
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

Those carve-outs are the whole purpose of the ``on_behalf_post_mode``
setting: it keeps the user in control of their colleague-visible voice
while letting the agent draft freely, self-document on its own work, and
answer reviewers on the owner's own MRs.

The DB-home ``on_behalf_post_mode`` setting (default
:attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK`, per-overlay
overridable, env override via ``T3_ON_BEHALF_POST_MODE``) is set with
``t3 <overlay> config_setting set on_behalf_post_mode <value>``; a
``[teatree]`` / ``[overlays.<name>]`` TOML value is ignored on read. This module
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
    Returned under :attr:`~teatree.config.OnBehalfPostMode.IMMEDIATE` for
    every action (including draft-form actions).
*   :attr:`OnBehalfVerdict.BLOCK` — no recorded approval matched, the
    caller must NOT publish; it surfaces the blocked post to the user (the
    user-notify path) so the user can record an approval in plain text.
    Returned for every colleague-**visible** action under
    :attr:`~teatree.config.OnBehalfPostMode.ASK` and
    :attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK`. A draft-form
    action NEVER yields BLOCK — it is exempt from the gate.
*   :attr:`OnBehalfVerdict.AUTO_DRAFT` — the action is a draft-form post
    (colleague-invisible, revocable) and the caller proceeds autonomously
    while recording a DM to the user with the publish/delete commands.
    Returned for actions in :data:`_DRAFT_FORM_ACTIONS` under BOTH
    :attr:`~teatree.config.OnBehalfPostMode.ASK` and
    :attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK` (drafts are
    exempt under every blocking mode, not just the default).
"""

from dataclasses import dataclass
from enum import StrEnum

from teatree.config import OnBehalfPostMode, get_effective_settings


@dataclass(frozen=True, slots=True)
class OnBehalfContext:
    """What the verdict knows about a post's DESTINATION, beyond its action.

    *overlay* is the overlay the post is addressed to — its ``on_behalf_post_mode``
    is the one that governs. Resolved ambiently instead, the mode is the invoking
    cwd's: on a multi-overlay install nothing resolves, the shipped
    :attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK` applies, and a repo whose
    owning overlay is pinned :attr:`~teatree.config.OnBehalfPostMode.IMMEDIATE` is
    refused by an overlay with no claim on it.

    *target* is the repo the post addresses, ``""`` when the caller names none.
    It is what separates "no target" (ambient resolution, unchanged) from a NAMED
    target no single overlay owns — an unowned target and a tie between overlays
    alike. That case has no PER-OVERLAY tier of its own to read, so it drops that
    one tier (see :attr:`scope`) instead of inheriting an ``immediate`` pin from an
    overlay with no claim on it; the recorded-approval channel is still its satisfier.

    *own_mr* is the caller's PROVED owner-authorship of the target MR. Every field
    defaults to the value that changes nothing, so an unset one never widens the gate.
    """

    overlay: str | None = None
    own_mr: bool = False
    target: str = ""

    @property
    def target_unowned(self) -> bool:
        """A NAMED target that resolved to no single overlay."""
        return bool(self.target) and not self.overlay

    @property
    def scope(self) -> str | None:
        """The overlay whose per-overlay tier the mode is read from, if any.

        ``""`` for an unowned NAMED target: the name of no overlay, which
        :func:`~teatree.config.get_effective_settings` resolves as "only the global
        DB scope applies". Dropping THAT ONE TIER is the whole of what "unowned"
        means — env still applies (a ``T3_ON_BEHALF_POST_MODE`` the operator set for
        the session is not an overlay's opinion, and silently retiring it is the
        #3895 failure in miniature, permissively as readily as restrictively), the
        global scope still applies (a workspace default has a claim on every repo),
        and with neither set the shipped ``DRAFT_OR_ASK`` is what remains.

        ``None`` — the ambient resolution — when no target is named at all.
        """
        return "" if self.target_unowned else self.overlay


class OnBehalfVerdict(StrEnum):
    """The three outcomes :func:`resolve_on_behalf_verdict` returns."""

    PROCEED = "proceed"
    BLOCK = "block"
    AUTO_DRAFT = "auto_draft"


# Actions that publish in a colleague-INVISIBLE, revocable draft form.
# These are EXEMPT from the on-behalf gate under *every* mode: a draft is
# never visible to colleagues (only the user can submit it), so it needs
# no approval — that is the whole point of the gate, which exists to keep
# the user in control of their colleague-VISIBLE voice. A draft-form
# action therefore never BLOCKs; it resolves to AUTO_DRAFT under ASK /
# DRAFT_OR_ASK (post the draft autonomously + DM the user the
# publish/delete commands) and to PROCEED under IMMEDIATE. Every action
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
# user pinned it), this one action BLOCKs regardless of ``on_behalf_post_mode`` —
# the customer-overlay done-definition gate.
_REVIEW_REQUEST_POST_ACTION: str = "review_request_post"


def resolve_on_behalf_verdict(action: str, context: OnBehalfContext | None = None) -> OnBehalfVerdict:
    """Return the verdict for *action* under the effective on-behalf mode.

    The gate covers colleague-**VISIBLE** posts only. Three carve-outs proceed
    without an approval even under the blocking modes:

    *   an action in the resolved ``on_behalf_auto_actions`` allowlist
        (default ``["post_e2e_evidence"]``) → :attr:`OnBehalfVerdict.PROCEED`
        under every mode (the user's own self-documentation, never a
        colleague-facing voice).
    *   a draft-form action (one of :data:`_DRAFT_FORM_ACTIONS`) is
        colleague-invisible and revocable, so it is exempt under every mode
        and never BLOCKs → :attr:`OnBehalfVerdict.AUTO_DRAFT` under
        :attr:`~teatree.config.OnBehalfPostMode.ASK` /
        :attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK` (post the
        draft autonomously and DM the user the publish/delete commands),
        and :attr:`OnBehalfVerdict.PROCEED` under
        :attr:`~teatree.config.OnBehalfPostMode.IMMEDIATE`.
    *   an author-side action (one of :data:`_AUTHOR_SIDE_ACTIONS`) with
        ``context.own_mr`` PROVED true → :attr:`OnBehalfVerdict.PROCEED`: the owner
        answering a reviewer on the owner's own MR. *own_mr* is a fact only
        the caller can establish (a forge read of the MR's author against the
        configured identity) and defaults ``False``, so an unproven or
        unprovable authorship keeps the post gated. It is inert for every
        action outside the set — passing ``own_mr=True`` for ``approve``
        still BLOCKs.

    For every other colleague-visible action:

    *   :attr:`~teatree.config.OnBehalfPostMode.IMMEDIATE` →
        :attr:`OnBehalfVerdict.PROCEED`.
    *   :attr:`~teatree.config.OnBehalfPostMode.ASK` /
        :attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK` →
        :attr:`OnBehalfVerdict.BLOCK`.

    Resolution follows the standard env (``T3_ON_BEHALF_POST_MODE``) →
    target-overlay → global → default chain via
    :func:`teatree.config.get_effective_settings`. The env layer applies on BOTH
    branches (``apply_env=True``): a named overlay otherwise drops it, which would
    silently retire ``T3_ON_BEHALF_POST_MODE`` — including an ``ask`` override,
    which must never stop applying.

    *context* carries what the caller knows about the post's destination (see
    :class:`OnBehalfContext`) — chiefly WHICH overlay's mode governs, via
    :attr:`OnBehalfContext.scope`. Omitted, the mode resolves ambiently, exactly as
    before; a NAMED target no single overlay owns drops the PER-OVERLAY tier only,
    landing on env → global → the shipped ``DRAFT_OR_ASK``. Forcing that case to
    ``DRAFT_OR_ASK`` outright would be the same silent retirement the paragraph
    above forbids, reintroduced one branch further down: a target no overlay
    enumerates is the COMMON case on a single-overlay install, so an operator's
    ``T3_ON_BEHALF_POST_MODE`` would stop applying almost everywhere.

    One mode-independent override sits above the table: when the resolved
    ``review_request_post_disabled`` is true, the single action
    ``review_request_post`` BLOCKs regardless of ``on_behalf_post_mode`` — even an
    explicitly pinned ``IMMEDIATE``. No autonomy tier reaches the mode (#3895):
    opening colleague egress is its own named opt-in, so an autonomous overlay
    still BLOCKs here on the shipped ``DRAFT_OR_ASK`` before the flag is read.
    The autonomy TIER drives the flag (#2579): the ``notify`` tier resolves it true
    (a collaborative/customer surface keeps a human in the merge loop and stops at
    "MR is mergeable + review-requestable", never auto-requesting review), while
    the ``full`` tier resolves it false (a solo tooling surface auto-requests). An
    explicit per-overlay pin always wins. It is scoped to that one action — every
    other colleague-visible post resolves through the table below unchanged.
    """
    context = context or OnBehalfContext()
    settings = get_effective_settings(context.scope, apply_env=True)
    mode = settings.on_behalf_post_mode
    # Mode-independent override: review-request posting is BLOCKed when the
    # resolved ``review_request_post_disabled`` is true (the ``notify`` tier sets
    # it, or the user pinned it), so this one action BLOCKs even under an
    # explicitly pinned ``on_behalf_post_mode = IMMEDIATE``. Scoped to
    # ``review_request_post`` — it never collapses any other action.
    if action == _REVIEW_REQUEST_POST_ACTION and settings.review_request_post_disabled:
        return OnBehalfVerdict.BLOCK
    if mode is OnBehalfPostMode.IMMEDIATE:
        return OnBehalfVerdict.PROCEED
    # Auto-proceed actions are the user's routine self-documentation on their
    # OWN ticket (E2E evidence) — not a colleague-facing voice — so they need
    # no per-post approval and proceed directly under every blocking mode.
    if action in settings.on_behalf_auto_actions:
        return OnBehalfVerdict.PROCEED
    # The owner answering a reviewer on the owner's OWN MR — their own voice
    # on their own work, exempted by owner decision (2026-07-23). Both
    # conditions must hold, so neither half can widen the carve-out alone.
    if context.own_mr and action in _AUTHOR_SIDE_ACTIONS:
        return OnBehalfVerdict.PROCEED
    # Draft-form actions are colleague-invisible — exempt from the gate
    # under every blocking mode (ASK and DRAFT_OR_ASK alike). They never
    # need approval; they auto-draft with a user DM receipt.
    if action in _DRAFT_FORM_ACTIONS:
        return OnBehalfVerdict.AUTO_DRAFT
    return OnBehalfVerdict.BLOCK


def on_behalf_post_will_block(action: str, context: OnBehalfContext | None = None) -> bool:
    """Whether *action* WILL BLOCK under the effective mode — the proactive pre-check.

    The forward-looking companion to :func:`resolve_on_behalf_verdict`: a caller
    runs this BEFORE attempting a colleague-visible on-behalf post so it can
    surface the owner's solution-oriented choice up front — enable the setting
    durably, or approve just this once (see
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
    return resolve_on_behalf_verdict(action, context) is OnBehalfVerdict.BLOCK
