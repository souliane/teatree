#!/usr/bin/env bash

# PreToolUse hook: blocks Bash/Edit/Write when suggested skills haven't been
# loaded yet.  Works with hook_router.py (UserPromptSubmit) which writes a .pending
# file listing the skills it told the agent to load.
#
# Exit 0 silently            → allow the tool call.
# JSON with deny on stdout   → block the tool call with a reason.

set -euo pipefail

input=$(cat)
session_id=$(echo "$input" | jq -r '.session_id // empty')
[ -z "$session_id" ] && exit 0

STATE_DIR="/tmp/claude-statusline"
pending_file="$STATE_DIR/${session_id}.pending"
skills_file="$STATE_DIR/${session_id}.skills"

# No pending suggestions → allow
[ ! -s "$pending_file" ] && exit 0

# Check each pending skill against loaded skills
unloaded=""
while IFS= read -r skill; do
    [ -z "$skill" ] && continue
    grep -qxF "$skill" "$skills_file" 2>/dev/null || unloaded="${unloaded} /${skill}"
done < "$pending_file"

# All loaded → allow
[ -z "$unloaded" ] && exit 0

# Block with reason
reason="SKILL LOADING ENFORCEMENT: You MUST load these skills first:${unloaded}. Call the Skill tool for each one BEFORE calling Bash/Edit/Write. The UserPromptSubmit hook told you to load them and you ignored it. This tool call is BLOCKED until you comply."
json_reason=$(printf '%s' "$reason" | jq -Rs .)
cat <<ENDJSON
{"permissionDecision": "deny", "permissionDecisionReason": ${json_reason}}
ENDJSON
