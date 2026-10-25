"""The ``block-ai-signature`` PreToolUse gate (#836, BLUEPRINT §17.6 gate 15), scanning in the hook process.

Split out of ``hook_router`` for module health; the router re-exports :func:`handle_block_ai_signature`
into ``_HANDLERS``. The matcher is stdlib-only (``teatree.hooks.ai_signature_scan``), so the gate imports
it from the sibling ``src/`` instead of spawning ``t3``: no container start, nothing for a stopped stack
to break. A scan that cannot run refuses a call that could publish a body, as a scanner error rather than
a finding, and lets every other call through with a stderr note. The deny and ``_resolve_cwd_repo`` are
back-imported lazily from the router.
"""

import re
import sys
from pathlib import Path

from hooks.scripts.banned_terms.gate import LOOKS_LIKE_A_PUBLISH_RE
from hooks.scripts.managed_repo import teatree_src_on_path as _teatree_src_on_path

sys.modules.setdefault("ai_signature_guard", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.ai_signature_guard", sys.modules[__name__])

_PR_CREATE_TOOLS = {
    "mcp__glab__glab_mr_create",
    "mcp__glab__glab_mr_update",
    "mcp__github__create_pull_request",
    "mcp__github__update_pull_request",
}

_GIT_COMMIT_RE = re.compile(r"\bgit\b[^|;&\n]*\bcommit\b")


def extract_bash_ai_sig_payload(command: str, cwd: Path | None = None) -> str | None:
    """Return the scannable forge-post body for a Bash command, or ``None``.

    Reuses the canonical command parser the quote-scanner, banned-terms and bare-reference gates
    share, so every forge-post command shape and every body flag form is covered. An unreadable,
    missing or binary body file contributes nothing (fail open), never a hard-block sentinel.
    The extraction lives in :func:`teatree.hooks.ai_signature_gate.extract_forge_post_body` so the
    parser's private import stays inside the ``teatree`` package.
    """
    from teatree.hooks.ai_signature_gate import extract_forge_post_body  # noqa: PLC0415 — deferred: cold-hook import

    return extract_forge_post_body(command, cwd)


def extract_ai_sig_payload(data: dict) -> str | None:
    """Return the PR-body / commit-message text to scan, else ``None`` (not a forge post, or no readable body)."""
    from hooks.scripts.hook_router import _resolve_cwd_repo  # noqa: PLC0415 — call-time back-import

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    if tool_name == "Bash":
        return extract_bash_ai_sig_payload(tool_input.get("command", ""), _resolve_cwd_repo(data))

    if tool_name in _PR_CREATE_TOOLS:
        return tool_input.get("body", "") or tool_input.get("description", "")

    return None


def _could_publish_a_body(data: dict) -> bool:
    """What the call looks like by its text alone, for the moment the scanner cannot run."""
    if data.get("tool_name") == "Bash":
        command = data.get("tool_input", {}).get("command", "")
        return LOOKS_LIKE_A_PUBLISH_RE.search(command) is not None or _GIT_COMMIT_RE.search(command) is not None
    return data.get("tool_name") in _PR_CREATE_TOOLS


def handle_block_ai_signature(data: dict) -> bool:
    """Refuse a forge-post body or commit message carrying an AI-signature trailer."""
    from hooks.scripts.hook_router import emit_pretooluse_deny  # noqa: PLC0415 — call-time back-import

    try:
        with _teatree_src_on_path():
            from teatree.hooks import ai_signature_gate, ai_signature_scan  # noqa: PLC0415 — cold-hook import

            payload = extract_ai_sig_payload(data)
            if payload is None:
                return False
            findings = ai_signature_scan.scan_text(payload)
            if not findings:
                return False
            message = ai_signature_gate.format_block_message(ai_signature_scan.summary(findings))
    except Exception as exc:  # noqa: BLE001 — an unscanned publish must fail closed
        cause = f"{type(exc).__name__}: {exc}"
        if not _could_publish_a_body(data):
            sys.stderr.write(f"[teatree] NOTE: AI-signature gate failed open ({cause}); this call publishes nothing.\n")
            return False
        return emit_pretooluse_deny(
            "BLOCKED: AI-signature scanner error — the PR body / commit message could not be confirmed "
            f"signature-free. This is a scanner error, not a detected trailer. {cause}",
            gate_id="ai_signature",
        )
    return emit_pretooluse_deny(message, gate_id="ai_signature")
