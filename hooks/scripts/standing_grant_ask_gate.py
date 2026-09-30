"""PreToolUse: block-standing-grant-ask — never ask the owner for a sign-off a standing grant holds.

The owner granted standing substrate autonomy and was still asked, per PR, to
approve merges that grant already covers (#2663). The decision core is
:mod:`teatree.core.gates.standing_grant_ask_gate` (Django-free phrasing); this
module reads the configured grant for the overlay the question names — by repo
identity first, the session's overlay as the fallback — and routes the deny
through the router's ``_fail_open_or_deny`` chain. It runs BEFORE the question
mirror, so a loop-driven ask is refused rather than deferred to the owner's Slack.

NEVER-LOCKOUT: a per-call ``[grant-ask-ok: <reason>]`` token in the question, the
``[teatree] standing_grant_ask_gate_enabled = false`` kill-switch
(``t3 <overlay> gate standing-grant-ask disable``), and a fail-open on every
unreadable grant keep this gate from wedging a session.
"""

import os
import sys
from typing import TYPE_CHECKING

from hooks.scripts.django_bootstrap import bootstrap_teatree_django
from hooks.scripts.gate_result import warn_gate_skipped
from hooks.scripts.managed_repo import teatree_src_on_path
from hooks.scripts.question_gates import ask_questions

if TYPE_CHECKING:
    from teatree.core.merge.substrate_standing import SubstrateStandingAuthorization

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# registers and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("standing_grant_ask_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.standing_grant_ask_gate", sys.modules[__name__])

_GATE_ID = "block-standing-grant-ask"


def _standing_grant_ask_gate_enabled() -> bool:
    """Whether the gate is enabled (default True); an explicit ``false`` is the kill-switch."""
    from hooks.scripts.hook_router import _teatree_bool_setting  # noqa: PLC0415 deferred back-import

    return _teatree_bool_setting("standing_grant_ask_gate_enabled", default=True)


def _load_core():  # noqa: ANN202 — returns a lazily-imported handle; annotating would pull the type to module scope
    """Import the decision core, or ``None`` on any failure so the caller fails OPEN."""
    try:
        with teatree_src_on_path():
            from teatree.core.gates import standing_grant_ask_gate as core  # noqa: PLC0415 — deferred: cold-hook import
    except Exception:  # noqa: BLE001 — a cold env without teatree fails OPEN, never tracebacks.
        return None
    return core


def _question_texts(data: dict) -> list[str]:
    return [str(q.get("question", "")) for q in ask_questions(data)]


def _session_overlay() -> str:
    from teatree.config.discovery import discover_active_overlay  # noqa: PLC0415 — deferred: needs the bootstrap

    if named := os.environ.get("T3_OVERLAY_NAME", "").strip():
        return named
    active = discover_active_overlay()
    return active.name if active is not None else ""


def _configured_grant(refs: tuple[str, ...]) -> "tuple[str, SubstrateStandingAuthorization] | None":
    """The overlay the question concerns and its configured grant, or None when it cannot be read."""
    if not bootstrap_teatree_django():
        return None
    try:
        from teatree.core.merge.substrate_standing import (  # noqa: PLC0415 — deferred: ORM/app-registry
            configured_substrate_grant,
            resolve_overlay_by_repo_identity,
        )

        overlay = resolve_overlay_by_repo_identity(*refs, fallback=_session_overlay())
        return overlay, configured_substrate_grant(overlay_name=overlay)
    except Exception:  # noqa: BLE001 — crash-proof hook: an unreadable grant fails OPEN, never breaks the call
        return None


def _deny_reason_for(core, texts: list[str]) -> str | None:  # noqa: ANN001 — duck-typed core handle
    """The refusal for these questions, or None when the gate allows them."""
    finding = core.find_standing_grant_ask(texts)
    if finding is None:
        return None
    if escape := next(filter(None, map(core.grant_ask_ok_reason, texts)), None):
        sys.stderr.write(f"NOTE: standing-grant-ask gate skipped via [grant-ask-ok: {escape}].\n")
        return None
    resolved = _configured_grant(core.repo_refs(" ".join(texts)))
    if resolved is None:
        warn_gate_skipped("standing-grant-ask", "the overlay's standing substrate grant could not be read")
        return None
    overlay, grant = resolved
    return core.deny_reason(finding, overlay=overlay, delegated_by=grant.delegated_by) if grant else None


def handle_block_standing_grant_ask(data: dict) -> bool:
    """Deny an ``AskUserQuestion`` seeking a substrate-merge sign-off a configured standing grant holds.

    Returns ``True`` (deny emitted) only when the phrasing matches AND the grant
    is configured for the overlay; ``False`` (allow) otherwise, including on
    every resolution failure.
    """
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 deferred back-import

    if data.get("tool_name", "") != "AskUserQuestion" or not _standing_grant_ask_gate_enabled():
        return False
    core = _load_core()
    reason = _deny_reason_for(core, _question_texts(data)) if core is not None else None
    return _fail_open_or_deny(data, reason, gate_id=_GATE_ID) if reason else False


__all__ = ["handle_block_standing_grant_ask"]
