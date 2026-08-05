"""Verify an acting phase's result came from a run that could actually act.

The sibling of :mod:`teatree.agents.landing_verification`, one step earlier. That
gate asks whether a commit LANDED; this one asks whether the agent ACTED at all.
Both questions are needed because the landing gate is answerable only against a
base, and a long-lived branch is permanently ahead of its own — a bootstrap branch
thousands of commits ahead of an ``Initial commit`` base makes
``worktree_has_commits_ahead`` true forever, so the landing gate cannot fire and
``attempt_recorder._salvage_coding_result`` synthesizes ``files_modified`` from the
WHOLE branch diff. A toolless run that emitted one sentence of prose then records
``outcome=success`` carrying every file in the repo as its evidence.

The tool stream is the signal that survives that, because it is a property of the
RUN rather than of the branch. It also separates the two cases that matter:

* **Nothing to do** — a coding task whose change is already upstream. Reaching that
    conclusion requires reading the code, so the run emits tool calls and passes.
* **Could not act** — the agent had no usable tool surface, or stopped before
    touching one. Zero tool calls, and no way to have learned anything.

``None`` is neither: it means the recorder path never observed a tool stream (the
in-session ``record-attempt`` hand-off relays a sub-agent's envelope). "Not
measured" is never "did not act", so it returns no error — the same posture as
:func:`teatree.agents.landing_verification.landing_verification_error` on a ticket
with no checkable worktree.
"""

from teatree.core.modelkit.phases import normalize_phase

#: Phases whose deliverable is a change to the tree. A run on one of these that
#: touched no tool produced its answer without looking at anything.
_ACTION_VERIFIED_PHASES = frozenset({"coding", "debugging"})

_UNVERIFIED_PREFIX = "action_unverified:"


def action_verification_error(phase: str, *, tool_calls: int | None) -> str:
    """Return an ``action_unverified:`` error if an acting run made no tool call, else ``""``.

    Refuses only on a MEASURED zero for an acting phase. A non-acting phase, a
    positive count, and an unmeasured count are all ``""``.
    """
    if normalize_phase(phase) not in _ACTION_VERIFIED_PHASES:
        return ""
    if tool_calls is None or tool_calls > 0:
        return ""
    return (
        f"{_UNVERIFIED_PREFIX} the {phase} run made no tool call — it never read, wrote, or ran anything, "
        "so its result reports work it could not have done"
    )
