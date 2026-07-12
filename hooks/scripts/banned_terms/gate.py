"""PreToolUse: refuse a non-commit publish whose body carries a banned term (#1415).

The commit-only ``check-banned-terms.sh`` pre-commit hook misses ``gh
issue/pr create|edit|comment``, ``glab mr|issue note|create`` and the ``gh
api`` / ``glab api`` REST posting paths — exactly where overlay/customer terms
have leaked on this PUBLIC repo. This gate (sibling of the #1213 quote-scanner
gate) reuses the #1213 publish-surface detection + body extraction, then
delegates the matching to the SAME ``check-banned-terms.sh`` term list, denying
via ``permissionDecision: deny`` when a banned term reaches a public surface.

Extracted whole from ``hook_router`` (the #2384 Wave-2 router split, PR2) so the
8800-LOC dispatcher shrinks; the router re-exports
:func:`handle_banned_terms_pretool` into its ``_HANDLERS`` chain unchanged.

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with
no guarantee ``teatree`` is importable, so the module top imports only stdlib and
already-extracted ``hooks/scripts`` siblings (``teatree_settings`` /
``banned_terms.deny`` / ``banned_terms.marker`` / ``managed_repo``) — never Django
/ ``teatree.core``. The pure ``teatree.hooks`` leaves (``banned_terms_scanner`` /
``publish_destination`` / ``publish_surface``) stay function-scoped, imported only
after the handler puts the sibling ``src/`` on ``sys.path``. The shared spine
helpers ``emit_pretooluse_deny`` and ``_resolve_cwd_repo`` stay in the router and
are back-imported lazily inside the handler bodies — the ``hooks/scripts`` sibling
back-import the import-direction fitness test permits (it governs only the
``src/teatree/hooks`` leaves).

The ``src`` bootstrap is the SHARED ``managed_repo.teatree_src_on_path`` context
manager, NOT a hand-rolled ``parents[N] / "src"`` computation. This module lives
one level deeper than its ``hooks/scripts`` siblings
(``hooks/scripts/banned_terms/``), so a per-file ``parents`` index is off-by-one
relative to theirs; routing through the one shared helper — which resolves ``src``
relative to ITS OWN location, correct for a caller at any depth — is what keeps the
bootstrap from silently pointing at a nonexistent ``hooks/src`` and fail-opening
the leak gate on a cold host (HLG-1/HLG-5).
"""

import sys
from pathlib import Path

from hooks.scripts.banned_terms.deny import emit_banned_term_deny
from hooks.scripts.banned_terms.marker import resolve_marker as _resolve_banned_terms_marker
from hooks.scripts.managed_repo import teatree_src_on_path as _teatree_src_on_path
from hooks.scripts.teatree_settings import teatree_bool_setting as _teatree_bool_setting


def _banned_terms_gate_enabled() -> bool:
    """Whether the #1415 banned-terms publish gate is enabled (default True).

    Fails OPEN to enabled on a missing/broken config so the gate keeps its
    protective default; an explicit ``[teatree] banned_terms_gate_enabled =
    false`` is the one-line kill-switch (NEVER-LOCKOUT) the user flips to
    disable the gate while its body-resolution over-block (an allowlisted
    private-repo commit hard-blocked because the body could not be read) is
    fixed properly. See :func:`_teatree_bool_setting` for the shared semantics.
    """
    return _teatree_bool_setting("banned_terms_gate_enabled", default=True)


def handle_banned_terms_pretool(data: dict) -> bool:
    """Refuse a non-commit publish whose body carries a banned term.

    Sibling of the #1213 quote-scanner gate. The commit-only
    ``check-banned-terms.sh`` pre-commit hook misses ``gh issue/pr
    create|edit|comment``, ``glab mr|issue note|create`` and the
    ``gh api`` / ``glab api`` REST posting paths — exactly where
    overlay/customer terms have leaked on this PUBLIC repo. This gate
    reuses the #1213 ``_command_parser`` publish-surface detection + body
    extraction, then delegates the matching to the SAME
    ``check-banned-terms.sh`` against the DB ``banned_terms`` list
    (no new term config, no reimplemented matching).

    A banned-term match ⇒ refuse via ``permissionDecision: deny`` + a
    reason naming the matched term and pointing at the
    ``--allow-banned-term`` / ``ALLOW_BANNED_TERM=1`` override.

    Fail-open on any internal error: a crashing hook is worse than no scan
    (never-lockout). But the fail-open is NOT silent — an unscanned body on the
    PUBLIC-egress publish path is exactly the leak this gate exists to catch, so an
    internal error is named loudly on stderr (HLG-3) rather than swallowed into an
    invisible no-op. The handler puts the sibling ``src/`` on ``sys.path`` via the
    shared :func:`managed_repo.teatree_src_on_path` bootstrap (the hook script runs
    in the user's session shell with no guarantee ``teatree`` is already importable,
    #1314); the shared helper resolves ``src`` relative to its own ``hooks/scripts``
    location, so this deeper-nested caller can never drift into the off-by-one that
    pointed the bootstrap at a nonexistent ``hooks/src`` (HLG-1/HLG-5).
    """
    if not _banned_terms_gate_enabled():
        return False
    try:
        with _teatree_src_on_path():
            return _run_banned_terms_pretool(data)
    except Exception as exc:  # noqa: BLE001 — fail-open on ANY error is the never-lockout contract
        sys.stderr.write(
            "[teatree] NOTE: banned-terms publish gate failed open on an internal error "
            f"({type(exc).__name__}: {exc}); the publish body was NOT scanned for banned terms. "
            "This is a fail-open safeguard (a crashing hook is worse than no scan), NOT a clean "
            "scan — fix the underlying error, or verify the body by hand before it reaches a "
            "public surface.\n"
        )
        return False


_BANNED_TERMS_CREDENTIAL_DENY = (
    "BLOCKED: a high-confidence secret (token / key / private-key block) was detected in the "
    "publish payload. Secrets are blocked on every surface, including a private repo — remove "
    "the credential before posting."
)


def _banned_term_marker_blocks(term: str, command: str, cwd_repo: Path | None) -> bool | None:
    """Decide a fail-closed MARKER term, or ``None`` when ``term`` is a real banned term.

    Thin router wrapper over ``banned_terms_marker.resolve_marker`` (which owns the
    destination-aware logic + rationale). For a real configured term it returns
    ``None`` so the caller takes its own destination-aware banned-term path. For a
    fail-closed marker the verdict is either a downgrade-to-warn (write the stderr
    line, return ``False``) or a hard-block (``emit_pretooluse_deny``).
    """
    from hooks.scripts.hook_router import emit_pretooluse_deny  # noqa: PLC0415 deferred back-import

    verdict = _resolve_banned_terms_marker(term, command, cwd_repo)
    if not verdict.is_marker:
        return None
    if verdict.warning is not None:
        sys.stderr.write(verdict.warning)
        return False
    return emit_pretooluse_deny(verdict.deny_message or "")


def _run_banned_terms_pretool(data: dict) -> bool:
    """Banned-terms inner body — assumes ``teatree`` is already importable."""
    from typing import cast  # noqa: PLC0415

    from hooks.scripts.hook_router import _resolve_cwd_repo, emit_pretooluse_deny  # noqa: PLC0415 deferred back-import
    from teatree.hooks import banned_terms_scanner, public_visibility, publish_surface  # noqa: PLC0415

    tool_name = data.get("tool_name", "")
    raw_input = data.get("tool_input", {}) or {}
    if not isinstance(raw_input, dict):
        return False
    tool_input = cast("banned_terms_scanner.ToolInput", raw_input)

    command = tool_input.get("command", "")
    cwd_repo = _resolve_cwd_repo(data)

    # A high-confidence secret leaks on EVERY surface -- a title, a short ``-t``
    # flag, a ``gh api -f title=`` field, a ``git -C ... commit`` subject -- not
    # only the description body, and on an internal post the destination gate
    # would SKIP or a command carrying the --allow-banned-term override. Scan the
    # WIDE surface set and block before the payload-None early-return and any skip
    # / override short-circuit (#1672 secrets-always-blocked invariant).
    if publish_surface.contains_secret(banned_terms_scanner.secret_scan_text(tool_name, tool_input)):
        return emit_pretooluse_deny(_BANNED_TERMS_CREDENTIAL_DENY)

    payload = banned_terms_scanner.extract_publish_payload(tool_name, tool_input, cwd_repo)
    if payload is None:
        return False

    skipped = banned_terms_scanner.has_override(tool_name, tool_input) or (
        tool_name == "Bash" and public_visibility.gate_skips_for_visibility(command, cwd_repo)
    )
    term = None if skipped else banned_terms_scanner.scan_text(payload)
    if term is None:
        return False
    marker_decision = _banned_term_marker_blocks(term, command, cwd_repo)
    if marker_decision is not None:
        return marker_decision
    return emit_banned_term_deny(tool_name, command, payload, term, cwd_repo)
