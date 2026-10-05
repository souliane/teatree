"""The one envelope that puts a hook's text in front of the model.

Claude Code reads ``additionalContext`` only nested under ``hookSpecificOutput`` beside the event's own
``hookEventName``; a top-level ``additionalContext`` is dropped as an unrecognized key. Stdlib only, so
any hook process can write it without importing the router.
"""

import contextlib
import json
import os
import sys
from pathlib import Path


def emit_additional_context(event: str, context: str) -> None:
    """Write *context* as the call's one stdout object, in the nested form *event* accepts, and flush it."""
    emit_hook_output({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}})


def emit_hook_output(output: dict) -> None:
    """Write *output* as the call's one stdout object and flush it.

    Returning means the object reached the pipe, so a caller records a delivery only after this returns.
    A write or flush that fails (a pipe the harness stopped reading, the caller's time budget) raises, and
    what was left unwritten is dropped: the interpreter's exit flush would fail on it again and turn a
    fail-open hook into a failed one.
    """
    try:
        json.dump(output, sys.stdout)
        sys.stdout.flush()
    except BaseException:
        _drop_unwritten()
        raise


def _drop_unwritten() -> None:
    with contextlib.suppress(OSError, ValueError), Path(os.devnull).open("wb") as devnull:
        os.dup2(devnull.fileno(), sys.stdout.fileno())


__all__ = ["emit_additional_context", "emit_hook_output"]
