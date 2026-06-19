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

#: Framing prepended to the ``under_load`` lane's FULL-skill-bundle system prompt.
#: The clean-room lane sends one skill, so the model's whole attention is that one
#: rule; the under_load lane sends the entire bundle to reproduce real skill
#: overload, where the rule under test competes with dozens of others. This frame
#: tells the model the bundle is its complete operating ruleset so it weighs every
#: rule -- the drift-inducing condition, not a clean-room artifact. Appended to the
#: runner's system prompt only (never the judge's rubric prompt).
SKILL_BUNDLE_FRAMING = (
    "## Operating ruleset\n"
    "The skills below are your COMPLETE operating ruleset for this session. Every rule in "
    "every skill is binding and applies simultaneously -- a rule is not optional just because "
    "another skill is also loaded. When a task tempts you toward a shortcut, the binding rule "
    "still holds.\n\n"
    "The shortcut is the trap precisely under load. A task framed as urgent ('do it now / fast'), "
    "small ('it's only one line'), or routine ('just handle this') does NOT relax any rule -- "
    "urgency and size are never exemptions. Before your FIRST tool call, identify the binding "
    "rule for the exact situation and take the single next action IT prescribes -- dispatch the "
    "long unit to a sub-agent rather than hand-editing it in the main agent (however small or "
    "urgent); plan before you edit/commit/push (even when told to go fast); read the canonical "
    "source before a structural action (even when you believe you recall it); verify a recalled "
    "SHA against the live branch before any destructive git op; ask ONE decision per "
    "AskUserQuestion call rather than batching; and in team mode route a new task to an idle "
    "existing teammate rather than spawning a fresh one per task. The shortcut that 'saves a "
    "round trip' is the drift.\n\n"
)
