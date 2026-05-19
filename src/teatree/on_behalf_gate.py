"""On-behalf posting pre-gate — tri-state resolver (#960).

Single source of truth for the *tri-state* setting that decides what
teatree does *before* publishing a post made under the user's identity
to a colleague/customer surface — a PR/MR comment, an issue comment, a
Slack channel/thread message, a Notion post, a PR/MR approval, or a
reaction on someone else's message.

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
    every action.
*   :attr:`OnBehalfVerdict.BLOCK` — no recorded approval matched, the
    caller must NOT publish; it surfaces the blocked post to the user (the
    user-notify path) so the user can record an approval in plain text.
    Returned under :attr:`~teatree.config.OnBehalfPostMode.ASK` for every
    action, and under :attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK`
    for every non-draft-form action.
*   :attr:`OnBehalfVerdict.AUTO_DRAFT` — the action is a draft-form post
    (colleague-invisible, revocable) and the caller proceeds autonomously
    while recording a DM to the user with the publish/delete commands.
    Returned only under
    :attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK` for actions in
    :data:`_DRAFT_FORM_ACTIONS`.

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


# Actions that publish in a colleague-invisible, revocable draft form
# and therefore qualify for the AUTO_DRAFT verdict under DRAFT_OR_ASK.
# Every other action collapses to BLOCK under DRAFT_OR_ASK (identical to
# ASK), so this set is the load-bearing per-action carve-out for the new
# default mode.
_DRAFT_FORM_ACTIONS: frozenset[str] = frozenset({"post_draft_note"})


def resolve_on_behalf_verdict(action: str) -> OnBehalfVerdict:
    """Return the verdict for *action* under the effective on-behalf mode.

    *   :attr:`~teatree.config.OnBehalfPostMode.IMMEDIATE` → always
        :attr:`OnBehalfVerdict.PROCEED`.
    *   :attr:`~teatree.config.OnBehalfPostMode.ASK` → always
        :attr:`OnBehalfVerdict.BLOCK`.
    *   :attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK` →
        :attr:`OnBehalfVerdict.AUTO_DRAFT` for actions in
        :data:`_DRAFT_FORM_ACTIONS`, :attr:`OnBehalfVerdict.BLOCK`
        otherwise.

    Resolution follows the standard env (``T3_ON_BEHALF_POST_MODE``) →
    active-overlay → global → default chain via
    :func:`teatree.config.get_effective_settings`.
    """
    mode = get_effective_settings().on_behalf_post_mode
    if mode is OnBehalfPostMode.IMMEDIATE:
        return OnBehalfVerdict.PROCEED
    if mode is OnBehalfPostMode.ASK:
        return OnBehalfVerdict.BLOCK
    # DRAFT_OR_ASK
    if action in _DRAFT_FORM_ACTIONS:
        return OnBehalfVerdict.AUTO_DRAFT
    return OnBehalfVerdict.BLOCK


_DEPRECATION_EMITTED = False


def ask_before_post_on_behalf_enabled() -> bool:
    """Deprecated boolean shim — prefer :func:`resolve_on_behalf_verdict`.

    Returns ``True`` when the resolved mode is ASK or DRAFT_OR_ASK (both
    of which BLOCK every action that isn't a draft-form post), ``False``
    when IMMEDIATE. Emits a :class:`DeprecationWarning` on first call —
    new code should call :func:`resolve_on_behalf_verdict` directly so
    the tri-state distinction (BLOCK vs AUTO_DRAFT under DRAFT_OR_ASK)
    isn't lost.
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
