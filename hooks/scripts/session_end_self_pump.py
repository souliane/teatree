"""Release the per-agent consolidation slot + marker on session exit (#786 WS4).

Counterpart to the Stop self-pump: a clean exit drops both the actor-keyed
anti-spin marker and this session's consolidation registry entries, so a
fresh session of the same agent can re-claim immediately instead of waiting
for pid-liveness to expire.

Extracted from the over-cap ``hook_router.py`` (docs/module-health.md) rather
than for any concern of its own — the internals it needs stay back-imported
from the origin so a test patching them there still steers this handler.
"""

import sys

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# re-exports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("session_end_self_pump", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.session_end_self_pump", sys.modules[__name__])


def handle_session_end_self_pump(data: dict) -> None:
    """Release the per-agent consolidation slot + marker on session exit."""
    from hooks.scripts.hook_router import (  # noqa: PLC0415 — deferred: call-time back-import
        _actor_key,
        _release_agent_consolidation_slot,
        _state_file,
    )

    session_id = data.get("session_id", "")
    if not session_id:
        return
    _state_file(_actor_key(data), "pump-armed").unlink(missing_ok=True)
    _release_agent_consolidation_slot(session_id)
