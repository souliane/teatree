"""Slack listener-downtime catch-up: replay what Slack holds since the listener was last alive.

:func:`catch_up` is the one entry point. It reads the owner's bot DM (history, replies
under thread roots active in the window, the owner's reactions on every walked message)
and hands each item to *deliver*, the path a live Socket Mode event takes, so a replayed
item and a live one deduplicate against each other. Only
``teatree.loops.slack_catchup_job`` may import this package (tach-fenced).
"""

from teatree.slack_catchup.replay import CatchUpSlice, catch_up

__all__ = ["CatchUpSlice", "catch_up"]
