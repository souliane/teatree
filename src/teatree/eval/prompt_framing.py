"""Live-environment framing for the SDK eval runner's per-scenario system prompt.

The clean-room runner (:mod:`teatree.eval.sdk_runner`) uses ONLY the scenario's
skill as the system prompt to isolate the skill's effect, so it lacks the "you
are in a live environment, use your tools" framing real Claude Code usage
supplies. Without it the model narrates the correct action as TEXT instead of
issuing the tool call -- a clean-room artifact, not a skill defect.

:data:`LIVE_ENV_FRAMING` is appended to the RUNNER's system prompt only (never
the judge's rubric prompt, which is built separately in :mod:`teatree.eval.judge`
and must not be told to "issue tool calls"). Anti-vacuity is untouched: the
deterministic ``_fail`` / ``_noop`` fixtures are REPLAYED (not SDK-run), so a
wrong action still grades RED regardless of this framing.
"""

LIVE_ENV_FRAMING = (
    "\n\n## Environment\n"
    "You are in a LIVE environment with working tools. When the task calls for an action, "
    "perform it by issuing the actual tool call -- never print the command as text or describe "
    'what you "would" do. If the task is genuinely underspecified (a needed URL/path/id is '
    "missing), ask instead of guessing."
)
