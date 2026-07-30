"""Router-side I/O for a resolved quote-scanner verdict (#2384 router split, #F7.9).

Extracted whole from ``hook_router`` so the shrink-only dispatcher stays under
its LOC ceiling; the router re-imports :func:`quote_scanner_high_block_message`
unchanged. This owns ONLY the ledger + stderr I/O for an already-resolved
:class:`~hooks.scripts.quote_verdict.QuoteVerdict` — the stderr warning + JSONL
ledger write on a warn-downgrade, or the ledger deny + the block MESSAGE on a
deny. It returns the block message (a plain ``str``) rather than calling the
router's ``emit_pretooluse_deny`` writer, so this module has NO dependency back
on the router (no lazy back-import): the router keeps the single deny chokepoint
and the repeated-denial circuit breaker. The verdict DECISION itself
(#1213/#1415/#126) lives in the ``quote_verdict`` sibling; the quote-scanner
gate's ``sys.path`` bootstrap and its fail-open exception handler (#F7.9) stay in
the router's ``handle_quote_scanner_pretool``.

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with
no guarantee ``teatree`` is importable, so the module top imports only stdlib.
"""

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

    from hooks.scripts.quote_verdict import QuoteVerdict


def quote_scanner_high_block_message(
    quote_scanner: "ModuleType", tool_name: str, result: object, verdict: "QuoteVerdict"
) -> str | None:
    """Apply a resolved quote ``QuoteVerdict`` and return the deny message, or ``None``.

    ``None`` on a warn-downgrade (the publish proceeds after a stderr warning +
    ledger write); the block MESSAGE on a deny (after the ledger deny), which the
    caller hands to the router's ``emit_pretooluse_deny`` chokepoint. Mirrors
    ``_banned_term_marker_blocks``.
    """
    if verdict.warning is not None:
        sys.stderr.write(verdict.warning)
        quote_scanner.log_decision(tool_name=tool_name, decision=verdict.decision, result=result, override=False)
        return None
    quote_scanner.log_decision(tool_name=tool_name, decision="deny", result=result, override=False)
    return quote_scanner.format_block_message(result)
