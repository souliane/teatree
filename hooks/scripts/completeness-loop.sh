#!/usr/bin/env bash
# SessionStart hook: register the teatree fat loop via CronCreate.
#
# The fat loop tick combines:
#  - Scanner dispatch and statusline render (t3 loop tick)
#  - Task completeness checks (TaskList, deferred-work scan)
#
# Singleton: if tick-meta.json was written within 2x the cadence,
# the loop is already registered in this or a recent session — skip.

_meta="${XDG_DATA_HOME:-$HOME/.local/share}/teatree/tick-meta.json"

if [ -f "$_meta" ]; then
    _cadence="${T3_LOOP_CADENCE:-720}"
    _meta_mtime=$(stat -f %m "$_meta" 2>/dev/null || stat -c %Y "$_meta" 2>/dev/null || echo 0)
    _age=$(( $(date +%s) - _meta_mtime ))
    [ "$_age" -lt $(( _cadence * 2 )) ] && exit 0
fi

_cadence="${T3_LOOP_CADENCE:-720}"
_cron_minutes=$(( _cadence / 60 ))
[ "$_cron_minutes" -lt 1 ] && _cron_minutes=1

cat <<JSON
{
  "additionalContext": "MANDATORY SESSION SETUP (from teatree SessionStart hook): Create a recurring fat loop NOW by calling the CronCreate tool with these exact parameters: cron=\"*/${_cron_minutes} * * * *\", recurring=true, prompt=\"Run \`t3 loop tick\` in Bash. Then call TaskList — flag any task in_progress >15 min without a commit, scan for deferred work. Report briefly.\". Do NOT skip this step — it is required for session health monitoring."
}
JSON
