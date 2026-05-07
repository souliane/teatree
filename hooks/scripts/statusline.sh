#!/usr/bin/env bash
# Claude Code statusline hook.
#
# Composes two info streams:
#  1. The fat loop's pre-rendered zones file (anchors, action_needed, in_flight)
#     written by `t3 loop tick` to ${TEATREE_STATUSLINE_FILE} or the default
#     XDG path. Decoupling render from read keeps this hook fast (<10ms).
#  2. Live per-session info from Claude's stdin JSON: model, context-window %,
#     and skills loaded this session — the latter populated by hook_router.py
#     / track-skill-usage.sh into ${state_dir}/<session_id>.skills.

set -u

target="${TEATREE_STATUSLINE_FILE:-${XDG_DATA_HOME:-$HOME/.local/share}/teatree/statusline.txt}"
state_dir="${TEATREE_CLAUDE_STATUSLINE_STATE_DIR:-/tmp/claude-statusline}"

session_id=""
model=""
ctx_pct=""
if ! [ -t 0 ] && command -v jq >/dev/null 2>&1; then
    input=$(cat)
    if [ -n "$input" ]; then
        session_id=$(printf '%s' "$input" | jq -r '.session_id // empty')
        model=$(printf '%s' "$input" | jq -r '.model.display_name // empty')
        ctx_pct=$(printf '%s' "$input" | jq -r '.context_window.used_percentage // empty' | cut -d. -f1)
    fi
fi

skills=""
if [ -n "$session_id" ]; then
    skills_file="$state_dir/${session_id}.skills"
    if [ -r "$skills_file" ]; then
        skills=$(paste -sd ' ' "$skills_file")
    fi
fi

header=""
sep=""
if [ -n "$model" ]; then
    header="model=${model}"
    sep=" | "
fi
if [ -n "$ctx_pct" ] && [ "$ctx_pct" != "empty" ]; then
    header="${header}${sep}ctx=${ctx_pct}%"
    sep=" | "
fi
if [ -n "$skills" ]; then
    header="${header}${sep}skills: ${skills}"
fi

[ -n "$header" ] && printf '%s\n' "$header"

if [[ -r "$target" ]]; then
    cat "$target"
fi
