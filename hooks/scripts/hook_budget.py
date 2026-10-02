"""The PreToolUse wall-clock ceiling every in-hook subprocess shares (souliane/teatree#4305).

``hooks.json`` gives a PreToolUse hook 30 seconds, and one handler can spend it
on more than one shellout. Gate 12 does: ``coverage_gate`` shells the ``t3 tool
diff-coverage`` measurement, then — only on a finding — ``existing_artifact``
shells the ``t3 tool open-pr`` probe. Their fixed timeouts summed to 45s inside
that 30s window because neither knew about the other.

Overrunning does not merely truncate the second call. The harness cancels the
hook, so NO ``permissionDecision`` is emitted at all and the guarded ``gh pr
create`` proceeds — the gate loses the deny it had already reached. A gate that
answers with less information is strictly better than one that answers with none.

So a timeout here is a REQUEST against what is left, never an entitlement:
:func:`bounded_timeout_s` shrinks it to what the ceiling still affords and
answers ``None`` once the budget is spent, so the caller skips a subprocess it
would only be cancelled inside of.

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with
no guarantee ``teatree`` / Django is importable, so the module top is stdlib only.
"""

import sys
from typing import Final

# Alias the bare and ``hooks.scripts.`` identities to ONE module object, as every
# sibling does, so the live hook's bare import and a test's package import share globals.
sys.modules.setdefault("hook_budget", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.hook_budget", sys.modules[__name__])

# Mirrors the PreToolUse ``"timeout"`` declared in ``hooks/hooks.json``, pinned to it
# by ``tests/test_hook_budget.py`` so lowering it there cannot silently outrun this.
HOOK_CEILING_S: Final[int] = 30

# The handler still has to serialise and write its decision after the last
# subprocess returns; spending the ceiling to the last millisecond loses it.
_EMIT_RESERVE_S: Final[float] = 1.0


def bounded_timeout_s(preferred: float, elapsed: float) -> float | None:
    """*preferred* seconds, shrunk to what the ceiling still affords after *elapsed*.

    ``None`` means the budget is spent — start no subprocess, because one the
    harness cancels costs the whole decision rather than just its own result. A
    backwards *elapsed* (a clock read the caller could not trust) counts as no
    time spent, which shortens nothing that was already safe.
    """
    remaining = HOOK_CEILING_S - _EMIT_RESERVE_S - max(0.0, elapsed)
    return min(preferred, remaining) if remaining > 0 else None
