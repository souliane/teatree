#!/usr/bin/env bash
# SessionStart hook: inject a mandatory completeness-check /loop directive.
# The agent MUST set up a recurring self-check loop at session start.
# This is deterministic: the hook fires every session, the directive is
# in additionalContext, and the /loop skill handles the scheduling.

cat <<'JSON'
{
  "additionalContext": "MANDATORY SESSION SETUP (from teatree SessionStart hook): Create a recurring completeness check NOW by running: /loop 10m — On each tick: (1) call TaskList, (2) flag any task in_progress >15 min without a commit, (3) flag any finding listed as 'deferred' or 'for another session', (4) if all tasks are completed and no deferrals exist, stop the loop. This loop prevents premature completion claims. Do NOT skip this step."
}
JSON
