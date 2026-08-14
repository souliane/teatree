"""Record what the operator said; refuse to publish it back verbatim (#4195).

Two handlers, one concern. ``handle_record_operator_message`` fingerprints each
inbound operator message on ``UserPromptSubmit``; ``handle_block_verbatim_
operator_paste`` refuses a ``gh``/``glab`` post whose body reproduces one. The
detection and the ledger live in the pure ``teatree.hooks.verbatim_paste`` leaf
— this module owns only the hook wiring, the destination scoping and the
``permissionDecision`` JSON.

Sibling of the #1415 banned-terms gate, and deliberately independent of it: a
term-list hit presents as a vocabulary problem, so clearing it reads as clearing
the concern. There is no token to add to a term list that expresses "this is
someone's private message".

Cold-import safe: the module top imports only stdlib plus the already-extracted
``hooks/scripts`` siblings, never Django / ``teatree.core``. The pure
``teatree.hooks`` leaves are imported inside the shared
``managed_repo.teatree_src_on_path`` bootstrap, which resolves ``src`` relative
to its OWN location and is therefore correct from any caller depth.
"""

import sys

from hooks.scripts.loop_prompt_shape import is_bare_loop_prompt
from hooks.scripts.managed_repo import teatree_src_on_path as _teatree_src_on_path
from hooks.scripts.skill_loader_input import strip_ambient_context
from hooks.scripts.teatree_settings import teatree_bool_setting as _teatree_bool_setting

_OVERRIDE_NOTE = (
    "NOTE: verbatim operator-paste gate (#4195) SKIPPED by an explicit "
    "ALLOW_VERBATIM_PASTE=1 override; the override is recorded.\n"
)


def _gate_enabled() -> bool:
    """Whether the #4195 gate is enabled (default True).

    Fails OPEN to enabled on a missing/broken config so the gate keeps its
    protective default; ``t3 <overlay> gate verbatim-paste disable`` is the
    never-lockout kill-switch.
    """
    return _teatree_bool_setting("verbatim_paste_gate_enabled", default=True)


def handle_record_operator_message(data: dict) -> None:
    """Fingerprint this prompt as an operator message (UserPromptSubmit).

    Harness-injected ambient blocks are stripped first: the CLAUDE.md body and
    the memory index arrive inside every prompt, and fingerprinting them would
    make quoting the repo's own documentation look like quoting the operator.
    A bare loop tick is machine text, not operator speech, and is skipped.

    Silent and crash-proof — an unwritable ledger degrades the later publish
    check to UNKNOWN (which announces itself), never blocks the prompt.
    """
    session_id = str(data.get("session_id", ""))
    prompt = data.get("prompt", "")
    if not session_id or not isinstance(prompt, str) or not prompt:
        return
    try:
        if is_bare_loop_prompt(prompt):
            return
        with _teatree_src_on_path():
            from teatree.hooks import verbatim_paste  # noqa: PLC0415 — cold-hook import after the src bootstrap

            verbatim_paste.record_operator_message(strip_ambient_context(prompt), session_id=session_id)
    except Exception:  # noqa: BLE001 — crash-proof hook: recording never blocks a prompt
        return


def handle_block_verbatim_operator_paste(data: dict) -> bool:
    """Refuse a forge post whose body reproduces the operator's own words.

    Fail-open on any internal error, but never silently: an unscanned body on
    the public-egress path is exactly the leak this gate exists to catch, so the
    failure is named on stderr rather than swallowed into an invisible no-op.
    """
    if not _gate_enabled():
        return False
    try:
        with _teatree_src_on_path():
            return _run_verbatim_paste_pretool(data)
    except Exception as exc:  # noqa: BLE001 — fail-open on ANY error is the never-lockout contract
        sys.stderr.write(
            "[teatree] NOTE: verbatim operator-paste gate (#4195) failed open on an internal error "
            f"({type(exc).__name__}: {exc}); the publish body was NOT checked against the operator's "
            "own messages. This is a fail-open safeguard, NOT a clean scan.\n"
        )
        return False


def _public_publish_payload(data: dict) -> str | None:
    """The body this call would publish to a PUBLIC forge surface, else ``None``.

    Scoped to the forge posting verbs (issue/PR create, edit, comment) — the
    public surfaces #4195 names. A local commit is not a publish, and a
    provably-private destination has no public-leak surface.
    """
    from typing import cast  # noqa: PLC0415 — deferred: off the fast hook's load path

    from hooks.scripts.hook_router import _resolve_cwd_repo  # noqa: PLC0415 deferred back-import
    from teatree.hooks import banned_terms_scanner, public_visibility, publish_surface  # noqa: PLC0415 — cold-hook read

    raw_input = data.get("tool_input", {}) or {}
    if data.get("tool_name", "") != "Bash" or not isinstance(raw_input, dict):
        return None
    tool_input = cast("banned_terms_scanner.ToolInput", raw_input)
    command = tool_input.get("command", "")
    if not publish_surface.is_gh_glab_posting_command(command):
        return None
    cwd_repo = _resolve_cwd_repo(data)
    if public_visibility.gate_skips_for_visibility(command, cwd_repo):
        return None
    return banned_terms_scanner.extract_publish_payload("Bash", tool_input, cwd_repo)


def _run_verbatim_paste_pretool(data: dict) -> bool:
    """Inner body — assumes ``teatree`` is already importable."""
    from hooks.scripts.hook_router import emit_pretooluse_deny  # noqa: PLC0415 deferred back-import
    from teatree.hooks import verbatim_paste  # noqa: PLC0415 — cold-hook read after the src bootstrap

    payload = _public_publish_payload(data)
    if payload is None:
        return False
    command = (data.get("tool_input", {}) or {}).get("command", "")
    verdict = verbatim_paste.scan_body(payload, session_id=str(data.get("session_id", "")))
    if verbatim_paste.has_override(command):
        verbatim_paste.log_decision(decision="override", verdict=verdict)
        sys.stderr.write(_OVERRIDE_NOTE)
        return False
    if verdict.outcome == verbatim_paste.UNKNOWN:
        verbatim_paste.log_decision(decision="unknown", verdict=verdict)
        sys.stderr.write(verbatim_paste.format_unknown_message(verdict))
        return False
    if verdict.outcome == verbatim_paste.REPRODUCED:
        verbatim_paste.log_decision(decision="blocked", verdict=verdict)
        return emit_pretooluse_deny(verbatim_paste.format_block_message(verdict))
    return False
