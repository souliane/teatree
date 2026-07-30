"""Live-environment framing for the SDK eval runner's per-scenario system prompt.

The clean-room runner (:mod:`teatree.eval.api_runner`) uses ONLY the scenario's
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
)

#: Framing appended to the RUNNER's system prompt for a scenario whose toolset
#: exposes the ``Agent`` SPAWN tool (``teatree.eval.toolset``). The runner registers
#: exactly one sub-agent -- the bounded ``delegate`` stub from
#: :func:`~teatree.eval.toolset.build_delegation_agents` (``haiku``, ``maxTurns=1``,
#: reply-and-STOP) -- so a delegation scenario measures the main agent's DISPATCH
#: without the delegated unit actually executing.
#:
#: That bound is reached ONLY when the spawn NAMES the stub. Measured against the
#: bundled CLI: a spawn that omits ``subagent_type`` runs the UNBOUNDED built-in
#: ``general-purpose`` agent (10 tool uses, $0.2960), and registering a same-named
#: ``general-purpose`` entry in ``agents`` does NOT shadow that built-in; naming
#: ``subagent_type="delegate"`` reaches the stub (0 tool uses, $0.0400). So a spawn
#: that misses the stub runs the delegated unit FOR REAL inside the trial, and that
#: real work -- not the graded dispatch -- is what exhausts the per-scenario budget
#: cap, red-ing a CORRECT trajectory on a cap rather than a matcher (the #2192
#: cap-taint class).
#:
#: Naming the one registered sub-agent is an ENVIRONMENT fact, exactly like
#: :data:`LIVE_ENV_FRAMING`: it says nothing about WHETHER to delegate -- the
#: behaviour under test -- and no matcher inspects ``subagent_type``. Anti-vacuity
#: is untouched: the ``_fail`` / ``_noop`` fixtures are REPLAYED, not SDK-run.
DELEGATION_FRAMING = (
    "\n\n## Sub-agents\n"
    "Exactly one sub-agent type is registered in this environment: `delegate`. When you "
    'dispatch work with the Agent tool, pass `subagent_type: "delegate"` -- no other type '
    "exists here, and a spawn that omits it never reaches the registered sub-agent."
)
