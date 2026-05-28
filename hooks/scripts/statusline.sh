#!/usr/bin/env bash
# Claude Code statusline hook.
#
# Composes two info streams:
#  1. The fat loop's pre-rendered zones file (anchors, action_needed, in_flight)
#     written by `t3 loop tick` to ${TEATREE_STATUSLINE_FILE} or the default
#     XDG path. Decoupling render from read keeps this hook fast (<10ms).
#  2. Live per-session info from Claude's stdin JSON: model, context-window %,
#     5-hour and 7-day rate-limit usage, and skills loaded this session —
#     the latter populated by hook_router.py into
#     ${state_dir}/<session_id>.skills. Each loaded skill is expanded to its
#     resolved `requires:` dependency closure so the segment reflects the
#     full active set, not just explicitly tool-invoked names.

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
# Labels (`model=`, `ctx=`, separators…) used to use \033[2m (dim) which is
# unreadable on most themes. Switch to a regular light-gray that still reads
# as "metadata" without disappearing into the background.
_LBL=$'\033[38;5;244m'
_DIM=$'\033[38;5;244m'
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

# Visual grouping: within a group, segments are joined by a mid-dot; between
# groups we use a vertical bar so the eye can pick out context vs usage vs
# loops vs updates vs resource at a glance.
isep="${_LBL} · ${_RST}"
gsep="${_LBL} │ ${_RST}"
sep="$gsep"   # legacy alias still used by later segments

# Each `g_*` accumulates the colored content of one logical group. We join
# groups together at the end with the outer separator.
g_context=""
g_usage=""
g_loops=""
g_updates=""
g_resource=""

if [ -n "$model" ]; then
    g_context="${_LBL}model=${_RST}${_GRN}${model}${_RST}"
fi
if [ -n "$ctx_pct" ] && [ "$ctx_pct" != "empty" ]; then
    [ -n "$g_context" ] && g_context="${g_context}${isep}"
    g_context="${g_context}${_LBL}ctx=${_RST}$(color_pct "$ctx_pct")"
fi
if [ -n "$five_hour_pct" ] && [ "$five_hour_pct" != "empty" ]; then
    g_usage="${_LBL}5h=${_RST}$(color_pct "$five_hour_pct")$(format_reset_time "$five_hour_resets_at")"
fi
if [ -n "$seven_day_pct" ] && [ "$seven_day_pct" != "empty" ]; then
    [ -n "$g_usage" ] && g_usage="${g_usage}${isep}"
    g_usage="${g_usage}${_LBL}7d=${_RST}$(color_pct "$seven_day_pct")"
fi

# Skills are kept aside and tacked on last (or on their own line — see below)
# so they never push critical info off a narrow terminal.
_skills_segment=""
_skill_count=0
if [ -n "$skills" ]; then
    _colored_skills=""
    for _s in $skills; do
        # Skills already render as colored magenta tokens — a separator between
        # them is visual noise. Use a single space.
        [ -n "$_colored_skills" ] && _colored_skills="${_colored_skills} "
        _colored_skills="${_colored_skills}${_MAG}${_s}${_RST}"
        _skill_count=$((_skill_count + 1))
    done
    _skills_segment="${_LBL}skills:${_RST} ${_colored_skills}"
fi

# Active loops from CronCreate / ScheduleWakeup (written by hook_router.py)
_crons_file="$state_dir/${session_id}.crons"
_loops_segment=""
if [ -n "$session_id" ] && [ -r "$_crons_file" ] && command -v jq >/dev/null 2>&1; then
    _now=${_now:-$(date +%s)}
    _loop_parts=""
    for _jid in $(jq -r '.jobs // {} | keys[]' "$_crons_file" 2>/dev/null); do
        _jname=$(jq -r ".jobs[\"$_jid\"].name // \"loop\"" "$_crons_file" 2>/dev/null)
        _jcadence=$(jq -r ".jobs[\"$_jid\"].cadence // empty" "$_crons_file" 2>/dev/null)
        if [ -n "$_jcadence" ] && [ "$_jcadence" != "null" ]; then
            _jmin=$(( _jcadence / 60 ))
            _jlabel="${_CYN}${_jname}${_RST}${_LBL}(${_jmin}m)${_RST}"
        else
            _jcron=$(jq -r ".jobs[\"$_jid\"].cron // empty" "$_crons_file" 2>/dev/null)
            _jlabel="${_CYN}${_jname}${_RST}${_LBL}(${_jcron})${_RST}"
        fi
        [ -n "$_loop_parts" ] && _loop_parts="${_loop_parts}${isep}"
        _loop_parts="${_loop_parts}${_jlabel}"
    done
    _wakeup_epoch=$(jq -r '.wakeup.next_epoch // empty' "$_crons_file" 2>/dev/null)
    if [ -n "$_wakeup_epoch" ] && [ "$_wakeup_epoch" != "null" ]; then
        _wname=$(jq -r '.wakeup.name // "loop"' "$_crons_file" 2>/dev/null)
        _wdiff=$(( _wakeup_epoch - _now ))
        # The wakeup epoch is written only by ScheduleWakeup and has no clear
        # path: a long-finished wakeup would otherwise keep rendering as a
        # live "→now" forever. Treat anything more than the grace window
        # overdue as stale and omit it. A wakeup within the grace window
        # (0 to GRACE seconds overdue) still shows "now" — a real imminent
        # fire, not a stale leftover.
        _wakeup_stale_grace=120
        if (( _wdiff > 60 )); then
            _wtiming="$(( _wdiff / 60 ))m"
        elif (( _wdiff > 0 )); then
            _wtiming="${_wdiff}s"
        elif (( _wdiff >= -_wakeup_stale_grace )); then
            _wtiming="now"
        else
            _wtiming=""
        fi
        if [ -n "$_wtiming" ]; then
            _wlabel="${_CYN}${_wname}${_RST}${_LBL}→${_wtiming}${_RST}"
            [ -n "$_loop_parts" ] && _loop_parts="${_loop_parts}${isep}"
            _loop_parts="${_loop_parts}${_wlabel}"
        fi
    fi
    [ -n "$_loop_parts" ] && _loops_segment="${_LBL}loops:${_RST} ${_loop_parts}"
fi

# RAM usage (macOS/Linux)
# NOTE(#962): this computation is slated to move into `teatree.system.memory`
# (`t3 tool memory --json`) as the single source consumed by both the statusline
# and a provision-path RAM auto-throttle. See souliane/teatree#962.
_ram_segment=""
if [[ "$OSTYPE" == "darwin"* ]]; then
    _ram_total=$(sysctl -n hw.memsize 2>/dev/null)
    if [ -n "$_ram_total" ]; then
        _vmstat=$(vm_stat 2>/dev/null)
        _page_sz=$(awk '/page size of/{gsub(/[^0-9]/,"",$8); print $8}' <<< "$_vmstat")
        _free=$(awk '/Pages free/{gsub(/\./,"",$3); print $3}' <<< "$_vmstat")
        _inact=$(awk '/Pages inactive/{gsub(/\./,"",$3); print $3}' <<< "$_vmstat")
        _ram_used=$(( _ram_total - (_free + _inact) * _page_sz ))
        _ram_pct=$(( _ram_used * 100 / _ram_total ))
        _ram_used_gb=$(awk "BEGIN{printf \"%.1f\", $_ram_used / 1073741824}")
        _ram_total_gb=$(awk "BEGIN{printf \"%.0f\", $_ram_total / 1073741824}")
        _ram_segment="${_LBL}ram=${_RST}$(color_pct "$_ram_pct")${_LBL} ${_ram_used_gb}/${_ram_total_gb}G${_RST}"
    fi
elif [ -r /proc/meminfo ]; then
    _ram_total_kb=$(awk '/MemTotal/{print $2}' /proc/meminfo)
    _ram_avail_kb=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
    _ram_used_kb=$(( _ram_total_kb - _ram_avail_kb ))
    _ram_pct=$(( _ram_used_kb * 100 / _ram_total_kb ))
    _ram_used_gb=$(awk "BEGIN{printf \"%.1f\", $_ram_used_kb / 1048576}")
    _ram_total_gb=$(awk "BEGIN{printf \"%.0f\", $_ram_total_kb / 1048576}")
    _ram_segment="${_LBL}ram=${_RST}$(color_pct "$_ram_pct")${_LBL} ${_ram_used_gb}/${_ram_total_gb}G${_RST}"
fi

# Free disk space on the volume holding $HOME (cross-platform via POSIX df).
# Colored by used% so it goes red as the disk fills, mirroring the RAM segment.
_disk_segment=""
_df_out=$(df -Pk "$HOME" 2>/dev/null)
if [ -n "$_df_out" ]; then
    _disk_avail_kb=$(awk 'NR==2{print $4}' <<< "$_df_out")
    _disk_used_pct=$(awk 'NR==2{gsub(/%/,"",$5); print $5}' <<< "$_df_out")
    if [[ "$_disk_avail_kb" =~ ^[0-9]+$ ]] && [[ "$_disk_used_pct" =~ ^[0-9]+$ ]]; then
        _disk_free_gb=$(awk "BEGIN{printf \"%.0f\", $_disk_avail_kb / 1048576}")
        _disk_segment="${_LBL}disk=${_RST}$(color_pct "$_disk_used_pct")${_LBL} ${_disk_free_gb}G free${_RST}"
    fi
fi

g_resource="$_ram_segment"
if [ -n "$_disk_segment" ]; then
    [ -n "$g_resource" ] && g_resource="${g_resource}${isep}"
    g_resource="${g_resource}${_disk_segment}"
fi

# Next tick countdown from tick-meta.json
_tick_meta="${target%.txt}-meta.json"
# Also check the canonical sidecar name written by tick.py
[ ! -r "$_tick_meta" ] && _tick_meta="$(dirname "$target")/tick-meta.json"
_tick_segment=""
_freshness_segment=""
if [ -r "$_tick_meta" ] && command -v jq >/dev/null 2>&1; then
    _next_epoch=$(jq -r '.next_epoch // empty' "$_tick_meta" 2>/dev/null)
    _tick_cadence=$(jq -r '.cadence // 720' "$_tick_meta" 2>/dev/null)
    if [ -n "$_next_epoch" ]; then
        _now=$(date +%s)
        _diff=$(( _next_epoch - _now ))
        _overdue=$(( -_diff ))
        if (( _diff > 0 && _diff < 60 )); then
            _tick_segment="${_CYN}tick${_RST}${_LBL}→${_diff}s${_RST}"
        elif (( _diff > 0 && _diff < 120 )); then
            _tick_segment="${_YLW}tick${_RST}${_LBL}→$(( _diff / 60 ))m${_RST}"
        elif (( _diff > 0 )); then
            _tick_segment="${_GRN}tick${_RST}${_LBL}→$(( _diff / 60 ))m${_RST}"
        elif (( _overdue > _tick_cadence * 2 )); then
            _tick_segment="${_RED}tick stale${_RST}"
        else
            _tick_segment="${_CYN}tick now${_RST}"
        fi
    fi

    # Repo freshness from tick-meta.json .freshness
    _freshness=$(jq -r '.freshness // empty' "$_tick_meta" 2>/dev/null)
    if [ -n "$_freshness" ] && [ "$_freshness" != "null" ] && [ "$_freshness" != "{}" ]; then
        _now=${_now:-$(date +%s)}
        _fresh_parts=""
        for _repo in $(echo "$_freshness" | jq -r 'keys[]' 2>/dev/null); do
            _behind=$(echo "$_freshness" | jq -r ".\"$_repo\".behind // -1" 2>/dev/null)
            _fetch_ep=$(echo "$_freshness" | jq -r ".\"$_repo\".fetch_epoch // 0" 2>/dev/null)
            _path=$(echo "$_freshness" | jq -r ".\"$_repo\".path // empty" 2>/dev/null)
            # Recompute behind inline when FETCH_HEAD has been touched since the
            # tick wrote this entry (e.g. a manual `git pull` in another terminal).
            # Cheap: one local `git rev-list` per repo, no network.
            if [ -n "$_path" ] && [ -f "$_path/.git/FETCH_HEAD" ]; then
                # Linux stat (-c) first, BSD/macOS (-f) fallback. The reverse
                # order silently produces wrong output on Linux because
                # `stat -f` exists there too with a different meaning.
                _disk_ep=$(stat -c %Y "$_path/.git/FETCH_HEAD" 2>/dev/null || stat -f %m "$_path/.git/FETCH_HEAD" 2>/dev/null || echo 0)
                if [ "$_disk_ep" -gt "$_fetch_ep" ] 2>/dev/null; then
                    _fresh_behind=$(git -C "$_path" rev-list HEAD..origin/main --count 2>/dev/null)
                    if [ -n "$_fresh_behind" ]; then
                        _behind="$_fresh_behind"
                        _fetch_ep="$_disk_ep"
                    fi
                fi
            fi
            _age=""
            if [ "$_fetch_ep" -gt 0 ] 2>/dev/null; then
                _age_s=$(( _now - _fetch_ep ))
                if (( _age_s < 3600 )); then
                    _age="$(( _age_s / 60 ))m"
                elif (( _age_s < 86400 )); then
                    _age="$(( _age_s / 3600 ))h"
                else
                    _age="$(( _age_s / 86400 ))d"
                fi
            fi
            if [ "$_behind" -ge 0 ] 2>/dev/null; then
                if (( _behind == 0 )); then _fc="$_GRN"
                elif (( _behind <= 5 )); then _fc="$_YLW"
                else _fc="$_RED"
                fi
                _label="${_fc}${_repo}${_RST}${_LBL}=${_RST}${_fc}${_behind}${_RST}"
                [ -n "$_age" ] && _label="${_label}${_LBL}(${_age})${_RST}"
            elif [ -n "$_age" ]; then
                _label="${_LBL}${_repo}=${_age}${_RST}"
            else
                continue
            fi
            [ -n "$_fresh_parts" ] && _fresh_parts="${_fresh_parts}${isep}"
            _fresh_parts="${_fresh_parts}${_label}"
        done
        [ -n "$_fresh_parts" ] && _freshness_segment="${_fresh_parts}"
    fi
fi

# Combine tick + loops into the "loops" group (operational signals).
g_loops="$_tick_segment"
if [ -n "$_loops_segment" ]; then
    [ -n "$g_loops" ] && g_loops="${g_loops}${isep}${_loops_segment}" || g_loops="$_loops_segment"
fi
g_updates="$_freshness_segment"

# Join all groups with the between-group separator.
header=""
for _g in g_context g_usage g_loops g_updates g_resource; do
    _val=$(eval "printf '%s' \"\${$_g}\"")
    [ -z "$_val" ] && continue
    if [ -z "$header" ]; then
        header="$_val"
    else
        header="${header}${gsep}${_val}"
    fi
done

# Skills inline only when ≤ 4 are loaded — otherwise they get their own line
# below so the main header stays readable in narrow terminals.
_skills_on_own_line=0
if [ -n "$_skills_segment" ]; then
    if [ "$_skill_count" -le 4 ]; then
        if [ -z "$header" ]; then
            header="$_skills_segment"
        else
            header="${header}${gsep}${_skills_segment}"
        fi
    else
        _skills_on_own_line=1
    fi
fi

[ -n "$header" ] && printf '%s\n' "$header"
[ "$_skills_on_own_line" = "1" ] && printf '%s\n' "$_skills_segment"

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
