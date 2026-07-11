"""``t3 loop`` — the reactive-loop lifecycle CLI (thin package facade).

The ``loop`` Typer group and its lifecycle helpers live in
:mod:`teatree.cli.loop.app`; the per-concern subcommands split out of it for
module-health live beside it (``claim_next``, ``drain_queue``, ``listing``,
``owner``, ``slack_answer``, ``state``). This package ``__init__`` only
re-exports the group so ``from teatree.cli.loop import loop_app`` is unchanged.
"""

from teatree.cli.loop.app import _self_improve_cadence_for_loop_slot, loop_app

__all__ = ["_self_improve_cadence_for_loop_slot", "loop_app"]
