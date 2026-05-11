#!/usr/bin/env bash
# Claude Code statusline hook.
#
# Composes two info streams:
#  1. The fat loop's pre-rendered zones file (anchors, action_needed, in_flight)
#     written by `t3 loop tick` to ${TEATREE_STATUSLINE_FILE} or the default
#     XDG path. Decoupling render from read keeps this hook fast (<10ms).
#  2. Live per-session info from Claude's stdin JSON: model, context-window %,
#     5-hour and 7-day rate-limit usage, and skills loaded this session —
#     the latter populated by hook_router.py / track-skill-usage.sh into
#     ${state_dir}/<session_id>.skills.

set -u

target="${TEATREE_STATUSLINE_FILE:-${XDG_DATA_HOME:-$HOME/.local/share}/teatree/statusline.txt}"
state_dir="${TEATREE_CLAUDE_STATUSLINE_STATE_DIR:-/tmp/claude-statusline}"

session_id=""
model=""
ctx_pct=""
five_hour_pct=""
five_hour_resets_at=""
seven_day_pct=""
seven_day_resets_at=""
if ! [ -t 0 ] && command -v jq >/dev/null 2>&1; then
    input=$(cat)
    if [ -n "$input" ]; then
        session_id=$(printf '%s' "$input" | jq -r '.session_id // empty')
        model=$(printf '%s' "$input" | jq -r '.model.display_name // empty')
        ctx_pct=$(printf '%s' "$input" | jq -r '.context_window.used_percentage // empty' | cut -d. -f1)
        five_hour_pct=$(printf '%s' "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty' | cut -d. -f1)
        five_hour_resets_at=$(printf '%s' "$input" | jq -r '.rate_limits.five_hour.resets_at // empty')
        seven_day_pct=$(printf '%s' "$input" | jq -r '.rate_limits.seven_day.used_percentage // empty' | cut -d. -f1)
        seven_day_resets_at=$(printf '%s' "$input" | jq -r '.rate_limits.seven_day.resets_at // empty')
    fi
fi

skills=""
if [ -n "$session_id" ]; then
    skills_file="$state_dir/${session_id}.skills"
    if [ -r "$skills_file" ]; then
        skills=$(paste -sd ' ' "$skills_file")
    fi
fi

_DIM=$'\033[0;37m'
_YLW=$'\033[1;33m'
_RED=$'\033[1;31m'
_RST=$'\033[0m'

color_pct() {
    local pct="$1"
    if (( pct >= 95 )); then printf "${_RED}%s%%${_RST}" "$pct"
    elif (( pct >= 80 )); then printf "${_YLW}%s%%${_RST}" "$pct"
    else printf "${_DIM}%s%%${_RST}" "$pct"
    fi
}

format_reset_time() {
    local resets_at="$1"
    [ -z "$resets_at" ] || [ "$resets_at" = "empty" ] && return
    local reset_time=""
    if [[ "$resets_at" =~ ^[0-9]+$ ]]; then
        if [[ "$OSTYPE" == "darwin"* ]]; then
            reset_time=$(date -j -r "$resets_at" "+%H:%M" 2>/dev/null)
        else
            reset_time=$(date -d "@$resets_at" "+%H:%M" 2>/dev/null)
        fi
    fi
    [ -n "$reset_time" ] && printf ' →%s' "$reset_time"
}

header=""
sep=""
if [ -n "$model" ]; then
    header="model=${model}"
    sep=" | "
fi
if [ -n "$ctx_pct" ] && [ "$ctx_pct" != "empty" ]; then
    header="${header}${sep}ctx=$(color_pct "$ctx_pct")"
    sep=" | "
fi
if [ -n "$five_hour_pct" ] && [ "$five_hour_pct" != "empty" ]; then
    header="${header}${sep}5h=$(color_pct "$five_hour_pct")$(format_reset_time "$five_hour_resets_at")"
    sep=" | "
fi
if [ -n "$seven_day_pct" ] && [ "$seven_day_pct" != "empty" ]; then
    header="${header}${sep}7d=$(color_pct "$seven_day_pct")"
    sep=" | "
fi
if [ -n "$skills" ]; then
    header="${header}${sep}skills: ${skills}"
fi

[ -n "$header" ] && printf '%s\n' "$header"

if [[ -r "$target" ]]; then
    cat "$target"
fi

# Chain context-mode plugin statusline (token savings line).
ctx_statusline=$(ls -d "$HOME"/.claude/plugins/cache/context-mode/context-mode/*/bin/statusline.mjs 2>/dev/null | sort -V | tail -1)
if [ -n "$ctx_statusline" ] && [ -n "${input:-}" ]; then
    printf '%s' "$input" | node "$ctx_statusline"
fi
