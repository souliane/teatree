#!/usr/bin/env bash

# PostToolUse hook: tracks which skills have been invoked in this session.
# Appends skill names to /tmp/claude-statusline/<session_id>.skills

input=$(cat)

session_id=$(echo "$input" | jq -r '.session_id // empty')
[ -z "$session_id" ] && exit 0

skill_name=$(echo "$input" | jq -r '.tool_input.skill // empty')
[ -z "$skill_name" ] && exit 0

# Strip any prefix (e.g., "ms-office-suite:pdf" -> "pdf") — keep full name for qualified names
state_dir="/tmp/claude-statusline"
mkdir -p "$state_dir"

skills_file="$state_dir/${session_id}.skills"

# Only append if not already tracked
if ! grep -qxF "$skill_name" "$skills_file" 2>/dev/null; then
    echo "$skill_name" >> "$skills_file"
fi

exit 0
