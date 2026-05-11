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

_CYN=$'\033[1;36m'
_GRN=$'\033[1;32m'
_YLW=$'\033[1;33m'
_RED=$'\033[1;31m'
_BLU=$'\033[1;34m'
_MAG=$'\033[1;35m'
_DIM=$'\033[2m'
_RST=$'\033[0m'
_OSC8=$'\033]8;'
_ST=$'\033\\'

color_pct() {
    local pct="$1"
    if (( pct >= 95 )); then printf "${_RED}%s%%${_RST}" "$pct"
    elif (( pct >= 80 )); then printf "${_YLW}%s%%${_RST}" "$pct"
    else printf "${_GRN}%s%%${_RST}" "$pct"
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
    [ -n "$reset_time" ] && printf " ${_DIM}(until %s)${_RST}" "$reset_time"
}

osc8_link() {
    printf '%s' "${_OSC8};${1}${_ST}${2}${_OSC8};${_ST}"
}

header=""
sep="${_DIM} | ${_RST}"
if [ -n "$model" ]; then
    header="${_DIM}model=${_RST}${_GRN}${model}${_RST}"
fi
if [ -n "$ctx_pct" ] && [ "$ctx_pct" != "empty" ]; then
    header="${header}${sep}${_DIM}ctx=${_RST}$(color_pct "$ctx_pct")"
fi
if [ -n "$five_hour_pct" ] && [ "$five_hour_pct" != "empty" ]; then
    header="${header}${sep}${_DIM}5h=${_RST}$(color_pct "$five_hour_pct")$(format_reset_time "$five_hour_resets_at")"
fi
if [ -n "$seven_day_pct" ] && [ "$seven_day_pct" != "empty" ]; then
    header="${header}${sep}${_DIM}7d=${_RST}$(color_pct "$seven_day_pct")"
fi
if [ -n "$skills" ]; then
    _colored_skills=""
    for _s in $skills; do
        [ -n "$_colored_skills" ] && _colored_skills="${_colored_skills} ${_DIM}|${_RST} "
        _colored_skills="${_colored_skills}${_MAG}${_s}${_RST}"
    done
    header="${header}${sep}${_DIM}skills:${_RST} ${_colored_skills}"
fi

[ -n "$header" ] && printf '%s\n' "$header"

if [[ -r "$target" ]]; then
    cat "$target"
fi

# Chain extra statusline scripts from [teatree] statusline_chain in
# ~/.teatree.toml. Each entry is a glob pattern; the latest match
# (sort -V) is run with the Claude stdin JSON piped in.
if [ -n "${input:-}" ]; then
    _toml="$HOME/.teatree.toml"
    if [ -r "$_toml" ]; then
        _in_chain=false
        while IFS= read -r _line; do
            if [[ "$_line" =~ ^statusline_chain ]]; then _in_chain=true; continue; fi
            $_in_chain || continue
            [[ "$_line" =~ ^\] ]] && break
            [[ "$_line" =~ ^[[:space:]]*\" ]] || continue
            _pat=$(printf '%s' "$_line" | sed 's/.*"\(.*\)".*/\1/')
            _pat="${_pat/#\~/$HOME}"
            _resolved=$(ls -d $_pat 2>/dev/null | sort -V | tail -1)
            [ -z "$_resolved" ] && continue
            case "$_resolved" in
                *.mjs|*.js) _runner="node" ;;
                *.py)       _runner="python3" ;;
                *)          _runner="bash" ;;
            esac
            printf '%s' "$input" | "$_runner" "$_resolved" 2>/dev/null
        done < "$_toml"
    fi
fi
