"""PreToolUse ALLOW verdict primitive (#3 — a sanctioned allow must exit 0).

The DENY counterpart (``emit_pretooluse_deny`` + ``_write_pretooluse_deny``)
stays in ``hook_router`` where the never-lockout contract's call-graph analysis
can see it. The ALLOW verdict is its own leaf here so the ``classifier_relax_gate``
cold hook can emit a sanctioned allow without importing the router: it writes the
nested ``hookSpecificOutput`` allow envelope Claude Code actually reads and
returns the distinct :data:`Verdict.ALLOW` sentinel, which ``main()`` breaks the
handler chain on and translates to exit 0 — never the deny exit 2.

Before #3 the sanctioned allow emitted a bare legacy ``{"permissionDecision":
"allow"}`` and returned ``True``; ``main()`` treats a ``True`` return as a deny
and translates it to ``sys.exit(2)``, so a human's one-shot classifier-relax
approval acted as a BLOCK (and the blocked attempt in the transcript then burned
the consume-once consent). The distinct sentinel is what lets ``main()`` tell an
allow (exit 0) from a deny (exit 2).

Cold-import safe: stdlib only, so a bare ``python3`` PreToolUse subprocess can
import it with no ``teatree`` on the path.
"""

import enum
import json
import sys

# Alias the bare and ``hooks.scripts.`` identities so a bare ``from
# pretooluse_verdict import Verdict`` (the cold hook) and a ``hooks.scripts``
# test import resolve to ONE module object — one ``Verdict`` enum class, so
# ``verdict is Verdict.ALLOW`` identity holds across both import paths.
sys.modules.setdefault("pretooluse_verdict", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.pretooluse_verdict", sys.modules[__name__])


class Verdict(enum.Enum):
    """A PreToolUse handler decision ``main()`` maps to a process exit code.

    Handlers return ``bool | Verdict | None``: ``True`` denies (chain breaks,
    exit 2), ``None`` / ``False`` make no decision (the chain continues to the
    next handler), and ``ALLOW`` is an explicit allow — it breaks the chain with
    its envelope already on stdout but exits 0, never the deny exit 2 (#3).
    """

    ALLOW = "allow"


def emit_pretooluse_allow() -> Verdict:
    """Write the nested ``hookSpecificOutput`` allow envelope; return ``Verdict.ALLOW``.

    The allow counterpart of ``emit_pretooluse_deny``. Claude Code honours a
    PreToolUse allow only when ``permissionDecision`` rides inside
    ``hookSpecificOutput`` — a bare legacy ``{"permissionDecision": "allow"}`` is
    ignored. The legacy flat keys ride alongside for in-process test consumers,
    mirroring ``_write_pretooluse_deny``.
    """
    payload = {
        # Legacy flat shape — kept for in-process consumers (existing handler
        # tests). Harmless to the harness because it ignores unknown top-level keys.
        "permissionDecision": "allow",
        # Modern shape — the one the harness actually reads.
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        },
    }
    json.dump(payload, sys.stdout)
    return Verdict.ALLOW
