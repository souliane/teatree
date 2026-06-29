"""Loop-side re-export of the session-scoped identity primitive (#1073).

The real implementation lives in :mod:`teatree.core.session_identity` —
the module-boundary graph forbids the core-only ``teatree.outbound_claim``
re-exporter from importing ``teatree.loop``, so ``core`` is the canonical
home. The loop callers (``loops_tick``, ``statusline``, ``tick``) import
``current_session_id`` / ``current_session_pid`` from here so the loop
package has a self-describing entry point for its own ownership identity.
"""

from teatree.core.session_identity import current_session_id, current_session_pid

__all__ = ["current_session_id", "current_session_pid"]
