"""Consolidated hook-side banned-terms publish gate (U17).

One coherent package for the PreToolUse banned-terms leak gate, previously three
scattered ``hooks/scripts/banned_terms_{gate,deny,marker}.py`` siblings: the
``gate`` (the ``handle_banned_terms_pretool`` entry point the router dispatches),
the ``deny`` emitter, and the ``marker`` (the ``ALLOW_BANNED_TERM=1`` escape
resolver). Behaviour-preserving move — scan/deny logic is unchanged; the public
entry point is re-exported here so ``from hooks.scripts.banned_terms import
handle_banned_terms_pretool`` keeps working.
"""

from hooks.scripts.banned_terms.deny import emit_banned_term_deny
from hooks.scripts.banned_terms.gate import handle_banned_terms_pretool
from hooks.scripts.banned_terms.marker import MarkerVerdict, resolve_marker

__all__ = [
    "MarkerVerdict",
    "emit_banned_term_deny",
    "handle_banned_terms_pretool",
    "resolve_marker",
]
