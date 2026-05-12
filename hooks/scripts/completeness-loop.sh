#!/usr/bin/env bash
# SessionStart hook: ensure a durable teatree fat loop exists.
#
# Checks ~/.claude/scheduled_tasks.json for an existing "t3 loop tick"
# entry. If found, stays silent (singleton). If absent, injects an
# additionalContext directive telling the agent to create one via
# CronCreate with durable: true.
#
# The fat loop tick combines:
#  - Scanner dispatch and statusline render (t3 loop tick)
#  - Task completeness checks (TaskList, deferred-work scan)

_tasks_file="$HOME/.claude/scheduled_tasks.json"
_marker="t3 loop tick"

if [ -f "$_tasks_file" ] && grep -q "$_marker" "$_tasks_file" 2>/dev/null; then
    exit 0
fi

_cadence="${T3_LOOP_CADENCE:-720}"
_cron_minutes=$(( _cadence / 60 ))
[ "$_cron_minutes" -lt 1 ] && _cron_minutes=1

cat <<JSON
{
  "additionalContext": "MANDATORY SESSION SETUP (from teatree SessionStart hook): A durable teatree fat loop is NOT yet registered. Create one NOW by calling the CronCreate tool with these exact parameters: cron=\"*/${_cron_minutes} * * * *\", durable=true, recurring=true, prompt=\"Run \`t3 loop tick\` in Bash. Then call TaskList — flag any task in_progress >15 min without a commit, and scan the conversation for deferred work ('for another session', 'out of scope', 'TODO', 'later'). Create TaskCreate entries for any discovered skipped items. Report briefly.\". Do NOT skip this step — it is required for session health monitoring."
}
JSON
