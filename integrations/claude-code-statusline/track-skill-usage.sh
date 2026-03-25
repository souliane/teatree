#!/usr/bin/env bash

# Tracks which skills have been invoked in this session.
# Appends skill names to /tmp/claude-statusline/<session_id>.skills
#
# Registered for TWO hook events (belt-and-suspenders — PostToolUse "Skill"
# is intermittently unreliable in some Claude Code sessions):
#
#   PostToolUse  matcher: "Skill"
#     input: { "session_id": "...", "tool_input": { "skill": "t3-debug" }, ... }
#
#   InstructionsLoaded  matcher: "skills"
#     input: { "session_id": "...", "skills": [{ "name": "t3-debug", ... }], ... }

input=$(cat)

session_id=$(echo "$input" | jq -r '.session_id // empty')
[ -z "$session_id" ] && exit 0

state_dir="/tmp/claude-statusline"
mkdir -p "$state_dir"
skills_file="$state_dir/${session_id}.skills"

# --- PostToolUse path: single skill from tool_input ---
skill_name=$(echo "$input" | jq -r '.tool_input.skill // empty')
if [ -n "$skill_name" ]; then
    if ! grep -qxF "$skill_name" "$skills_file" 2>/dev/null; then
        echo "$skill_name" >> "$skills_file"
    fi
    exit 0
fi

# --- InstructionsLoaded path: array of skill objects ---
skill_names=$(echo "$input" | jq -r '.skills[]?.name // empty' 2>/dev/null)
if [ -n "$skill_names" ]; then
    while IFS= read -r name; do
        [ -z "$name" ] && continue
        if ! grep -qxF "$name" "$skills_file" 2>/dev/null; then
            echo "$name" >> "$skills_file"
        fi
    done <<< "$skill_names"
fi

exit 0
