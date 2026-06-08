"""On-behalf posting pre-gate — tri-state resolver (#960).

Single source of truth for the *tri-state* setting that decides what
teatree does *before* publishing a colleague-**VISIBLE** post made under
the user's identity to a colleague/customer surface — a PR/MR comment, an
issue comment, a Slack channel/thread message, a Notion post, a PR/MR
approval, or a reaction on someone else's message.

The gate governs colleague-visible posts ONLY. A *draft*-form action
(:data:`_DRAFT_FORM_ACTIONS`, e.g. ``post_draft_note``) is the ungated
safe-by-default: a draft is never visible to colleagues — only the user
can submit it — so it needs no approval under any mode and is exempt from
the gate entirely. That exemption is the whole purpose of the
``on_behalf_post_mode`` setting: it keeps the user in control of their
colleague-visible voice while letting the agent draft freely.

The setting is ``[teatree] on_behalf_post_mode`` (default
:attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK`, per-overlay
overridable, env override via ``T3_ON_BEHALF_POST_MODE``). This module
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

The legacy boolean helper :func:`ask_before_post_on_behalf_enabled` is
kept as a deprecated shim returning ``True`` for ASK/DRAFT_OR_ASK and
``False`` for IMMEDIATE — it emits a :class:`DeprecationWarning` on
first call. Use :func:`resolve_on_behalf_verdict` instead.
"""

import warnings
from enum import StrEnum

from teatree.config import OnBehalfPostMode, get_effective_settings


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


def resolve_on_behalf_verdict(action: str) -> OnBehalfVerdict:
    """Return the verdict for *action* under the effective on-behalf mode.

    The gate covers colleague-**VISIBLE** posts only. A draft-form action
    (one of :data:`_DRAFT_FORM_ACTIONS`) is colleague-invisible and
    revocable, so it is exempt under every mode and never BLOCKs:

    *   draft-form action → :attr:`OnBehalfVerdict.AUTO_DRAFT` under
        :attr:`~teatree.config.OnBehalfPostMode.ASK` /
        :attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK` (post the
        draft autonomously and DM the user the publish/delete commands),
        and :attr:`OnBehalfVerdict.PROCEED` under
        :attr:`~teatree.config.OnBehalfPostMode.IMMEDIATE`.

    For every colleague-visible action:

    *   :attr:`~teatree.config.OnBehalfPostMode.IMMEDIATE` →
        :attr:`OnBehalfVerdict.PROCEED`.
    *   :attr:`~teatree.config.OnBehalfPostMode.ASK` /
        :attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK` →
        :attr:`OnBehalfVerdict.BLOCK`.

    Resolution follows the standard env (``T3_ON_BEHALF_POST_MODE``) →
    active-overlay → global → default chain via
    :func:`teatree.config.get_effective_settings`.
    """
    mode = get_effective_settings().on_behalf_post_mode
    if mode is OnBehalfPostMode.IMMEDIATE:
        return OnBehalfVerdict.PROCEED
    # Draft-form actions are colleague-invisible — exempt from the gate
    # under every blocking mode (ASK and DRAFT_OR_ASK alike). They never
    # need approval; they auto-draft with a user DM receipt.
    if action in _DRAFT_FORM_ACTIONS:
        return OnBehalfVerdict.AUTO_DRAFT
    return OnBehalfVerdict.BLOCK


_DEPRECATION_EMITTED = False


def ask_before_post_on_behalf_enabled() -> bool:
    """Deprecated boolean shim — prefer :func:`resolve_on_behalf_verdict`.

    Returns ``True`` when the resolved mode is ASK or DRAFT_OR_ASK (both
    of which BLOCK colleague-visible posts), ``False`` when IMMEDIATE.
    Note this boolean is per-mode, not per-action: it cannot express that
    a draft-form action is exempt and auto-drafts under ASK/DRAFT_OR_ASK.
    Emits a :class:`DeprecationWarning` on first call — new code should
    call :func:`resolve_on_behalf_verdict` directly so the per-action
    distinction (BLOCK for visible posts vs AUTO_DRAFT for drafts under
    both blocking modes) isn't lost.
    """
    global _DEPRECATION_EMITTED  # noqa: PLW0603 — module-level first-call flag
    if not _DEPRECATION_EMITTED:
        warnings.warn(
            "ask_before_post_on_behalf_enabled() is deprecated; "
            "use teatree.on_behalf_gate.resolve_on_behalf_verdict(action) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        _DEPRECATION_EMITTED = True
    return get_effective_settings().on_behalf_post_mode is not OnBehalfPostMode.IMMEDIATE
