"""Bot→user Slack notification helper (#963).

The user does not read the Claude CLI: answers, questions, and important
info the agent surfaces inside a CLI turn are invisible to them. This
helper provides a single, always-on egress for those three directions —
post as the **bot** to the user's DM (the same channel ``DailyDigest``
opens) so the message arrives in Slack outside the active session.

Out of scope of the on-behalf gates (#960 ``on_behalf_post_mode``
and #949 ``notify_on_post_on_behalf``): those govern posts the agent
makes *as the user* to a colleague/customer surface. ``notify_user`` is
the **bot** talking to its own operator — a different concern with a
different doctrine.

Usage:

.. code-block:: python

    from teatree.notify import notify_user, NotifyKind

    notify_user(
        "Backend tests are green on s-963; ready for review.",
        kind=NotifyKind.INFO,
        idempotency_key=f"session={sid};turn={n}",
    )

Returns ``True`` when the bot posted (or detected an idempotent
re-send), ``False`` when no bot is configured (no-op-safe; never raises
into the CLI turn).

Implementation lives in :mod:`teatree.core.notify` so ``teatree.core``
modules (e.g. :mod:`teatree.core.on_behalf_gate_recorded`) can call it
without creating a ``teatree.core → teatree.notify`` tach edge that
would cycle through ``teatree.notify → teatree.core``. This module is
the stable public surface; pre-existing imports like
``from teatree.notify import notify_user`` keep working unchanged.
"""

from teatree.core.notify import NotifyKind, notify_user

__all__ = ["NotifyKind", "notify_user"]
