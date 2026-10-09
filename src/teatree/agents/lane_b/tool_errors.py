"""The tool failures that are the MODEL's mistake, not teatree's.

An exception escaping a tool kills the whole dispatch. Nothing in
:data:`~teatree.agents.pydantic_ai_session._RUN_ERRORS` matches a tool's own error,
so it propagates out of ``Agent.run`` and lands as the driver's ``sdk_error``
FAILED-with-a-traceback. That is the RIGHT outcome for a defect in teatree's own
code, and the wrong one for the far commoner case: the model named a path outside
its jail, a file that is not there, a substring that is not in the file. Those are
correctable — the model needs the reason, not the run's death.

Every such failure is a :class:`ToolInputError`, which :class:`HardDenyToolset`
turns into the same bounded ``ModelRetry`` a hard deny gets — the model reads the
reason and adapts, and a model that keeps getting it wrong ends the run at the
retry cap instead of looping. Anything NOT in this family still propagates, so a
real teatree bug stays loud.

This module is a leaf on purpose: the capability modules that RAISE these and the
gating wrapper that CATCHES them all import it, and none imports another.
"""


class ToolInputError(Exception):
    """A tool call the MODEL got wrong — surfaced to it as a retryable tool error."""


#: The exception families :class:`~teatree.agents.lane_b.gating.HardDenyToolset`
#: converts into a bounded ``ModelRetry``. ``OSError`` is in here because every
#: OSError a File System tool can raise describes the PATH THE MODEL NAMED —
#: missing, a directory, unreadable — which the model can fix on the next turn.
#: ``UnicodeDecodeError`` is the model editing a binary file. Both are listed
#: explicitly rather than folded into a blanket ``Exception`` catch, so an
#: unforeseen failure keeps killing the run loudly rather than being retried
#: three times and then reported as a model mistake.
CORRECTABLE_TOOL_ERRORS: tuple[type[Exception], ...] = (ToolInputError, OSError, UnicodeDecodeError)
