#!/usr/bin/env bash
# Claude Code statusline hook.
#
# Composes two info streams:
#  1. The fat loop's pre-rendered zones file (loop line, anchors,
#     action_needed, in_flight) written by `t3 loop tick` to
#     ${TEATREE_STATUSLINE_FILE} or the default XDG path. Decoupling render
#     from read keeps this hook fast (<10ms). The single dedicated LOOP
#     line lives at the top of that file (live_loops_anchor) — the header
#     this hook builds carries NO loop/tick info (#130): loop state has
#     exactly one home, the loop line.
#  2. Live per-session info from Claude's stdin JSON: model (with the session's
#     `/effort` level rendered beside it — from the payload if present, else the
#     saved settings default), context-window %,
#     5-hour and 7-day rate-limit usage, skills loaded this session, a compact
#     summary of this session's harness TODO list, the live Agent-Teams roster
#     (the ACTIVE mates of the team this session leads, read from the harness
#     team config), and a
#     per-session t3-master badge — the skills summary is populated by
#     hook_router.py into ${state_dir}/<session_id>.skills, the TODO summary is
#     counted directly from the harness's OWN task store
#     (${CLAUDE_TASKS_DIR:-~/.claude/tasks}/<session_id>/*.json — teatree keeps
#     no mirror of it), the badge from loop-registry.json. The t3-master badge shows "you ✓" (green) when this
#     session owns the loop, "owner·pid<PID>" (yellow, neutral) when a
#     different session owns it, or "unclaimed" (dim) when the registry has
#     no live owner. Unlike the shared loop line, this badge is resolved
#     per-session so every terminal reflects its own ownership context.
#     Each loaded skill is expanded to its resolved `requires:` dependency
#     closure so the segment reflects the full active set, not just
#     explicitly tool-invoked names.

set -u

target="${TEATREE_STATUSLINE_FILE:-${XDG_DATA_HOME:-$HOME/.local/share}/teatree/statusline.txt}"
state_dir="${TEATREE_CLAUDE_STATUSLINE_STATE_DIR:-/tmp/claude-statusline}"

session_id=""
model=""
effort=""
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
        # The session's reasoning-effort (the `/effort` setting). The harness
        # statusline payload does not currently carry it, but read it here first
        # so the segment upgrades for free if a future payload exposes `.effort`
        # (or `.model.effort`); otherwise it falls back to the saved settings
        # default below.
        effort=$(printf '%s' "$input" | jq -r '(.effort // .model.effort) as $e | if ($e|type)=="object" then ($e.level // empty) elif ($e|type)=="string" then $e else empty end')
        ctx_pct=$(printf '%s' "$input" | jq -r '.context_window.used_percentage // empty' | cut -d. -f1)
        five_hour_pct=$(printf '%s' "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty' | cut -d. -f1)
        five_hour_resets_at=$(printf '%s' "$input" | jq -r '.rate_limits.five_hour.resets_at // empty')
        seven_day_pct=$(printf '%s' "$input" | jq -r '.rate_limits.seven_day.used_percentage // empty' | cut -d. -f1)
        seven_day_resets_at=$(printf '%s' "$input" | jq -r '.rate_limits.seven_day.resets_at // empty')
    fi
fi

if [ -n "$session_id" ] && [ ! -f "${state_dir}/${session_id}.teatree-active" ]; then
    exit 0
fi

# The canonical ConfigSetting store's GLOBAL `autoload` value, JSON-decoded:
# `true` / `false` / empty. Read-only via the sqlite3 CLI (so the statusline needs
# no importable teatree python), mirroring teatree.config.cold_reader's WAL
# fallback: `mode=ro` first (live writer, sidecars present), then `immutable=1`
# (quiescent WAL, no sidecars — `mode=ro` then errors). Fails silent (empty) on no
# sqlite3, a missing DB, or any read error.
_autoload_db_value() {
    command -v sqlite3 >/dev/null 2>&1 || return 0
    local db="${T3_CONFIG_DB:-${XDG_DATA_HOME:-$HOME/.local/share}/teatree/db.sqlite3}"
    [ -f "$db" ] || return 0
    local q="SELECT value FROM teatree_config_setting WHERE scope='' AND key='autoload' LIMIT 1;"
    sqlite3 "file:${db}?mode=ro" "$q" 2>/dev/null \
        || sqlite3 "file:${db}?immutable=1" "$q" 2>/dev/null \
        || return 0
}

# The canonical ConfigSetting store's GLOBAL `statusline_chain` (a JSON array of
# glob patterns), one element per line. Read-only via the sqlite3 CLI + `json_each`
# (so the statusline needs no importable teatree python), with the same WAL
# fallback as `_autoload_db_value`. Empty on no sqlite3, a missing DB, no row, or a
# non-array value.
_statusline_chain_db() {
    command -v sqlite3 >/dev/null 2>&1 || return 0
    local db="${T3_CONFIG_DB:-${XDG_DATA_HOME:-$HOME/.local/share}/teatree/db.sqlite3}"
    [ -f "$db" ] || return 0
    local q="SELECT je.value FROM teatree_config_setting t, json_each(t.value) je WHERE t.scope='' AND t.key='statusline_chain';"
    sqlite3 "file:${db}?mode=ro" "$q" 2>/dev/null \
        || sqlite3 "file:${db}?immutable=1" "$q" 2>/dev/null \
        || return 0
}

# Session-start loop/statusline auto-load is OPT-IN (#256): default OFF so a
# colleague who merely clones the repo never sees the loop statusline. ``autoload``
# is the ONE owner flag (it engages the session AND arms its loops). Mirrors
# hook_router._autoload_enabled — env T3_AUTOLOAD first, then the canonical
# ConfigSetting DB read via the sqlite3 CLI (_autoload_db_value). eliminate-~/.teatree.toml:
# autoload is DB-home, so a [teatree] autoload TOML value is ignored on read (no TOML
# fallback); fails closed (silent OFF) on absence.
autoload_enabled() {
    local env_val="${T3_AUTOLOAD:-}"
    if [ -n "$env_val" ]; then
        case "$(printf '%s' "$env_val" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
            1|true|yes|on) return 0 ;;
            *) return 1 ;;
        esac
    fi
    case "$(_autoload_db_value)" in
        true) return 0 ;;
    esac
    return 1
}

if [ -n "$session_id" ] && ! autoload_enabled; then
    exit 0
fi

skills=""
todos_done=""
todos_total=""
todos_wip=""
if [ -n "$session_id" ]; then
    skills_file="$state_dir/${session_id}.skills"
    if [ -r "$skills_file" ]; then
        skills=$(paste -sd ' ' "$skills_file")
    fi
    # This session's harness TODO list, counted directly from the harness's
    # OWN on-disk task store — one ``<n>.json`` per todo with a ``status``
    # field, under ``$CLAUDE_TASKS_DIR/<session>/`` (default ~/.claude/tasks).
    # Teatree does NOT mirror that store (the old ``<session>.todos``
    # materialiser was removed); this reads the harness store the same way the
    # PreCompact snapshot's ``read_harness_todos`` does. Rendered as a
    # fixed-width ``TODO done/total ✓ · Nwip`` summary — never item content, so
    # width is bounded no matter how many todos exist. Distinct from the loop
    # work queue (rendered Python-side); this is the current session's checklist.
    # Fails open (empty chip) without jq or when the store dir is absent.
    tasks_dir="${CLAUDE_TASKS_DIR:-$HOME/.claude/tasks}/${session_id}"
    if [ -d "$tasks_dir" ] && command -v jq >/dev/null 2>&1; then
        _counts=$(jq -rs '
            map(select(type == "object") | .status // "pending") as $s
            | "\($s | length) \($s | map(select(. == "completed")) | length) \($s | map(select(. == "in_progress")) | length)"
        ' "$tasks_dir"/*.json 2>/dev/null || true)
        _total=$(printf '%s' "$_counts" | cut -d' ' -f1)
        _total=${_total:-0}
        if [ "$_total" -gt 0 ] 2>/dev/null; then
            todos_total="$_total"
            todos_done=$(printf '%s' "$_counts" | cut -d' ' -f2)
            todos_wip=$(printf '%s' "$_counts" | cut -d' ' -f3)
            todos_done="${todos_done:-0}"
            todos_wip="${todos_wip:-0}"
        fi
    fi
fi

# Statusline render-age freshness gate. A frozen statusline (dead/stopped
# loop) is otherwise displayed verbatim and the reader sees a confident,
# hours-old loop line ("next tick 4m" that never comes). Mirrors the cutoff
# arithmetic in src/teatree/loop/statusline_staleness.py inline (this hook
# stays a fast, dependency-light read and cannot import Python) — the cutoff is
# max(2*cadence, 300s); the render age is the `rendered_at` epoch in
# tick-meta.json. tests/test_claude_statusline.py pins both implementations to
# the same boundary so they cannot drift. Fails open (no banner) on a missing
# sidecar / absent rendered_at / no jq, so a freshness probe never blanks the
# line. Computed here, emitted as the first output line below.
_stale_banner=""
_sl_meta="${target%.txt}-meta.json"
[ ! -r "$_sl_meta" ] && _sl_meta="$(dirname "$target")/tick-meta.json"
if [ -r "$_sl_meta" ] && command -v jq >/dev/null 2>&1; then
    _rendered_at=$(jq -r '.rendered_at // empty' "$_sl_meta" 2>/dev/null)
    _sl_cadence=$(jq -r '.cadence // empty' "$_sl_meta" 2>/dev/null)
    if [[ "$_rendered_at" =~ ^[0-9]+$ ]]; then
        [[ "$_sl_cadence" =~ ^[0-9]+$ ]] || _sl_cadence=720
        _sl_cutoff=$(( 2 * _sl_cadence ))
        [ "$_sl_cutoff" -lt 300 ] && _sl_cutoff=300
        _sl_age=$(( $(date +%s) - _rendered_at ))
        if [ "$_sl_age" -gt "$_sl_cutoff" ] 2>/dev/null; then
            if (( _sl_age < 3600 )); then _sl_age_h="$(( _sl_age / 60 ))m"
            elif (( _sl_age < 86400 )); then _sl_age_h="$(( _sl_age / 3600 ))h"
            else _sl_age_h="$(( _sl_age / 86400 ))d"
            fi
            _stale_banner=$'\033[1;31m'"⚠ statusline STALE — last rendered ${_sl_age_h} ago; loop may be stopped (re-register its /loop via /t3:loops, or run \`t3 loops tick\`)"$'\033[0m'
        fi
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

# The saved `/effort` default from the harness settings file
# (${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json .effortLevel). This is the
# fallback when the live statusline payload carries no effort field. Prints the
# value or nothing; fails open (empty) on a missing file / absent key / no jq so
# a broken settings file never blanks the statusline.
effort_from_settings() {
    command -v jq >/dev/null 2>&1 || return
    local cfg="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
    [ -r "$cfg" ] || return
    jq -r '.effortLevel // empty' "$cfg" 2>/dev/null
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
g_updates=""
g_resource=""

# Per-session t3-master badge — resolved from loop-registry.json so each
# terminal shows its own ownership context, not the shared t3-master chunk
# that live_loops_anchor() intentionally omits. Gated on jq + session_id;
# fails open (no badge) on any read error or missing registry.
_loop_owner_badge=""
if command -v jq >/dev/null 2>&1 && [ -n "${session_id:-}" ]; then
    _reg="${T3_LOOP_REGISTRY_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/teatree}/loop-registry.json"
    if [ -r "$_reg" ]; then
        _owner_raw=$(jq -r '."t3-loop-tick-owner" | "\(.session_id // "")\t\(.pid // "")"' "$_reg" 2>/dev/null || true)
        IFS=$'\t' read -r _owner_sid _owner_pid <<< "${_owner_raw:-	}"
        _owner_sid="${_owner_sid:-}"
        _owner_pid="${_owner_pid:-}"
        if [ "$_owner_sid" = "$session_id" ]; then
            _loop_owner_badge="${_LBL}t3-master:${_RST} ${_GRN}you ✓${_RST}"
        elif [ -n "$_owner_sid" ]; then
            _loop_owner_badge="${_LBL}t3-master:${_RST} ${_YLW}${_owner_sid:0:8}·pid${_owner_pid}${_RST}"
        else
            _loop_owner_badge="${_LBL}t3-master: unclaimed${_RST}"
        fi
    fi
fi

# Effort level (`/effort`): the live payload field above, else the saved
# settings default. Rendered as a short `· <effort>` suffix on the model chunk
# (e.g. `model=opus-4-8 · medium`); omitted entirely when unknown so the segment
# stays honest and leaves no dangling separator.
if [ -z "$effort" ]; then
    effort=$(effort_from_settings)
fi
if [ -n "$model" ]; then
    g_context="${_LBL}model=${_RST}${_GRN}${model}${_RST}"
    if [ -n "$effort" ]; then
        g_context="${g_context}${_LBL} · ${_RST}${_GRN}${effort}${_RST}"
    fi
fi
if [ -n "$ctx_pct" ] && [ "$ctx_pct" != "empty" ]; then
    [ -n "$g_context" ] && g_context="${g_context}${isep}"
    g_context="${g_context}${_LBL}ctx=${_RST}$(color_pct "$ctx_pct")"
fi
# The per-session t3-master badge does NOT go in g_context — it is
# loop-specific info and belongs on the loop line region (appended after the
# cat'd zones file below), so all loop state has one visual home.
if [ -n "$five_hour_pct" ] && [ "$five_hour_pct" != "empty" ]; then
    g_usage="${_LBL}5h=${_RST}$(color_pct "$five_hour_pct")$(format_reset_time "$five_hour_resets_at")"
fi
if [ -n "$seven_day_pct" ] && [ "$seven_day_pct" != "empty" ]; then
    [ -n "$g_usage" ] && g_usage="${g_usage}${isep}"
    g_usage="${g_usage}${_LBL}7d=${_RST}$(color_pct "$seven_day_pct")"
fi

# SDK-equivalent month-to-date spend, rendered immediately after the weekly
# (7d) rate-limit segment so the two usage windows read together. The dollar
# figure is computed Python-side and handed over via tick-meta.json's
# ``cost_chip`` (e.g. ``SDK mtd ≈$48/$200``); this hook only places it. Empty
# when no headless cost is captured this cycle or the sidecar is unreadable.
_cost_chip=""
_cost_meta="${target%.txt}-meta.json"
[ ! -r "$_cost_meta" ] && _cost_meta="$(dirname "$target")/tick-meta.json"
if [ -r "$_cost_meta" ] && command -v jq >/dev/null 2>&1; then
    _cost_chip=$(jq -r '.cost_chip // empty' "$_cost_meta" 2>/dev/null)
fi
if [ -n "$_cost_chip" ]; then
    [ -n "$g_usage" ] && g_usage="${g_usage}${isep}"
    g_usage="${g_usage}${_BLU}${_cost_chip}${_RST}"
fi

# Skills are kept aside and tacked on last (or on their own line — see below)
# so they never push critical info off a narrow terminal. Skills sharing a
# ``<ns>:`` prefix collapse into one ``ns:{a,b,c}`` token so a long t3:* set
# does not blow out the width; un-namespaced skills and lone-member namespaces
# render verbatim. Namespace order and member order follow first appearance.
_skills_segment=""
_skill_count=0
if [ -n "$skills" ]; then
    _ns_order=""
    _ns_members=""
    _plain_order=""
    for _s in $skills; do
        _skill_count=$((_skill_count + 1))
        if [[ "$_s" == *:* ]]; then
            _ns="${_s%%:*}"
            _member="${_s#*:}"
            case " $_ns_order " in
                *" $_ns "*) ;;
                *) _ns_order="${_ns_order}${_ns_order:+ }$_ns" ;;
            esac
            _ns_members="${_ns_members}${_ns_members:+$'\n'}${_ns}	${_member}"
        else
            _plain_order="${_plain_order}${_plain_order:+ }$_s"
        fi
    done

    _colored_skills=""
    for _ns in $_ns_order; do
        _members=""
        _member_count=0
        while IFS=$'\t' read -r _k _v; do
            [ "$_k" = "$_ns" ] || continue
            _members="${_members}${_members:+,}$_v"
            _member_count=$((_member_count + 1))
        done <<< "$_ns_members"
        if [ "$_member_count" -le 1 ]; then
            _token="${_ns}:${_members}"
        else
            _token="${_ns}:{${_members}}"
        fi
        [ -n "$_colored_skills" ] && _colored_skills="${_colored_skills} "
        _colored_skills="${_colored_skills}${_MAG}${_token}${_RST}"
    done
    for _p in $_plain_order; do
        [ -n "$_colored_skills" ] && _colored_skills="${_colored_skills} "
        _colored_skills="${_colored_skills}${_MAG}${_p}${_RST}"
    done
    _skills_segment="${_LBL}skills:${_RST} ${_colored_skills}"
fi

# Compact harness-TODO summary: ``TODO done/total ✓`` plus ``· Nwip`` only when
# work is in progress. Dimmed when every item is complete. Never lists item
# content, so the segment width is bounded regardless of list size.
_todo_segment=""
if [ -n "$todos_total" ] && [ "$todos_total" -gt 0 ] 2>/dev/null; then
    if [ "$todos_done" = "$todos_total" ]; then
        _todo_segment="${_DIM}TODO ${todos_done}/${todos_total} ✓${_RST}"
    else
        _todo_segment="${_LBL}TODO${_RST} ${_GRN}${todos_done}${_RST}${_LBL}/${todos_total} ✓${_RST}"
        if [ "$todos_wip" -gt 0 ] 2>/dev/null; then
            _todo_segment="${_todo_segment}${isep}${_YLW}${todos_wip}▸${_RST}"
        fi
    fi
fi

# Loop / tick info is intentionally NOT built here (#130). The single
# dedicated loop line (``<name> <Nm> · …``) is rendered by the fat loop into
# the zones file and cat'd below; duplicating it in this header is the
# pollution the dashboard rework removed.

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

# CPU load (macOS/Linux). A single non-delayed read of the 1-minute load
# average — never a multi-second sampler like `top -l 2`, so the hook stays
# fast. The load is normalized by core count and rendered as a percent so it
# reads alongside the RAM/disk indicators under the same color thresholds;
# above 100% means more runnable work than cores, which color_pct paints red.
_cpu_segment=""
_loadavg_raw=""
if [ -n "${TEATREE_STATUSLINE_LOADAVG_FILE:-}" ]; then
    [ -r "$TEATREE_STATUSLINE_LOADAVG_FILE" ] && _loadavg_raw=$(awk 'NR==1{print $1}' "$TEATREE_STATUSLINE_LOADAVG_FILE" 2>/dev/null)
elif [[ "$OSTYPE" == "darwin"* ]]; then
    _loadavg_raw=$(sysctl -n vm.loadavg 2>/dev/null | awk '{gsub(/[{}]/,""); print $1}')
elif [ -r /proc/loadavg ]; then
    _loadavg_raw=$(awk 'NR==1{print $1}' /proc/loadavg 2>/dev/null)
fi
_ncpu="${TEATREE_STATUSLINE_NCPU:-}"
if [ -z "$_ncpu" ]; then
    if [[ "$OSTYPE" == "darwin"* ]]; then
        _ncpu=$(sysctl -n hw.ncpu 2>/dev/null)
    else
        _ncpu=$(nproc 2>/dev/null)
    fi
fi
if [[ "$_loadavg_raw" =~ ^[0-9]+\.?[0-9]*$ ]] && [[ "$_ncpu" =~ ^[0-9]+$ ]] && [ "$_ncpu" -gt 0 ]; then
    _cpu_pct=$(awk "BEGIN{printf \"%.0f\", $_loadavg_raw * 100 / $_ncpu}")
    _cpu_segment="${_LBL}cpu=${_RST}$(color_pct "$_cpu_pct")"
fi

g_resource="$_ram_segment"
if [ -n "$_cpu_segment" ]; then
    [ -n "$g_resource" ] && g_resource="${g_resource}${isep}"
    g_resource="${g_resource}${_cpu_segment}"
fi
if [ -n "$_disk_segment" ]; then
    [ -n "$g_resource" ] && g_resource="${g_resource}${isep}"
    g_resource="${g_resource}${_disk_segment}"
fi

# Repo freshness from tick-meta.json. The next-tick countdown that used to
# live here is gone (#130): tick timing belongs on the single dedicated
# loop line, not split between this header and the loop line.
_tick_meta="${target%.txt}-meta.json"
# Also check the canonical sidecar name written by tick.py
[ ! -r "$_tick_meta" ] && _tick_meta="$(dirname "$target")/tick-meta.json"
_freshness_segment=""
if [ -r "$_tick_meta" ] && command -v jq >/dev/null 2>&1; then
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

g_updates="$_freshness_segment"

# Agent-Teams roster: the live mates of the team THIS session leads, rendered
# compactly so the lead sees who is on the bench without the harness's inline
# ``@mate · shift+↑/↓`` switcher being the only surface. The team config lives
# at ``<teams_dir>/<team>/config.json`` (teams_dir =
# ``${CLAUDE_CONFIG_DIR:-$HOME/.claude}/teams`` unless overridden by
# TEATREE_CLAUDE_TEAMS_DIR), keyed by team NAME not session — so we resolve the
# team by matching ``leadSessionId`` to this session and list ACTIVE members
# (``isActive == true``) other than the lead. Each mate is painted in its own
# ``color`` (the harness teammate color) when known, else neutral. Fails open
# (renders nothing, never errors) on no jq, no session id, no teams dir, no
# matching team, or any read/parse failure — a colleague who never runs a team
# sees exactly the statusline they always did.
_team_segment=""
if command -v jq >/dev/null 2>&1 && [ -n "${session_id:-}" ]; then
    _teams_dir="${TEATREE_CLAUDE_TEAMS_DIR:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/teams}"
    if [ -d "$_teams_dir" ]; then
        _mates_raw=""
        for _team_cfg in "$_teams_dir"/*/config.json; do
            [ -r "$_team_cfg" ] || continue
            # Only the team THIS session leads — its leadSessionId is ours. The
            # roster is rendered for the lead's terminal; a non-lead session
            # leads no team and so renders no roster.
            _is_my_team=$(jq -r --arg sid "$session_id" \
                'if (.leadSessionId // "") == $sid then "1" else "" end' \
                "$_team_cfg" 2>/dev/null)
            [ "$_is_my_team" = "1" ] || continue
            # One ``<color>\t<name>`` line per ACTIVE non-lead member. The lead
            # is excluded by agentId (it equals leadAgentId) so it never lists
            # itself even if a future config stamps it isActive.
            _mates_raw=$(jq -r '
                (.leadAgentId // "") as $lead
                | [ .members[]?
                    | select(((.isActive == true)) and ((.agentId // "") != $lead))
                    | "\(.color // "")\t\(.name // "")" ]
                | .[]' "$_team_cfg" 2>/dev/null)
            break
        done
        if [ -n "$_mates_raw" ]; then
            _mate_chips=""
            while IFS=$'\t' read -r _mate_color _mate_name; do
                [ -n "$_mate_name" ] || continue
                case "$_mate_color" in
                    red) _mc="$_RED" ;;
                    green) _mc="$_GRN" ;;
                    yellow) _mc="$_YLW" ;;
                    blue) _mc="$_BLU" ;;
                    magenta|purple|pink) _mc="$_MAG" ;;
                    cyan) _mc="$_CYN" ;;
                    *) _mc="$_GRN" ;;
                esac
                [ -n "$_mate_chips" ] && _mate_chips="${_mate_chips}${isep}"
                _mate_chips="${_mate_chips}${_mc}${_mate_name}${_RST}"
            done <<< "$_mates_raw"
            [ -n "$_mate_chips" ] && _team_segment="${_LBL}mates:${_RST} ${_mate_chips}"
        fi
    fi
fi
g_team="$_team_segment"

# Join all groups with the between-group separator. There is no loop group
# (#130) — loop/tick info has exactly one home, the dedicated loop line in
# the zones file cat'd below. The mates roster (g_team) rides the header as its
# own group, after resource, so it never crowds out model/ctx/usage.
header=""
for _g in g_context g_usage g_updates g_resource g_team; do
    _val=$(eval "printf '%s' \"\${$_g}\"")
    [ -z "$_val" ] && continue
    if [ -z "$header" ]; then
        header="$_val"
    else
        header="${header}${gsep}${_val}"
    fi
done

# The compact harness-TODO summary is its own header group: short, fixed-width,
# and per-session, so it rides the header without crowding skills onto a line.
if [ -n "$_todo_segment" ]; then
    if [ -z "$header" ]; then
        header="$_todo_segment"
    else
        header="${header}${gsep}${_todo_segment}"
    fi
fi

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

# The staleness banner (when the render is frozen) leads every other line so
# the reader sees the warning before the out-of-date content it qualifies.
[ -n "$_stale_banner" ] && printf '%s\n' "$_stale_banner"
[ -n "$header" ] && printf '%s\n' "$header"
[ "$_skills_on_own_line" = "1" ] && printf '%s\n' "$_skills_segment"

# The zones file holds the dedicated loop line (and the per-overlay anchors).
# The per-session t3-master badge is PREPENDED to that loop line so the user
# reads ownership first and all loop state shares one visual home. If the zones
# file has no loop line (loops not currently live), the badge is still surfaced
# on its own trailing line so per-session ownership context is never lost.
_zones_body=""
[[ -r "$target" ]] && _zones_body=$(cat "$target")
# The loop line is the FIRST line of the zones body when loops are live: it is
# always prepended above the per-overlay anchors, and every per-overlay anchor
# carries an ``[overlay]`` prefix the loop line lacks. So line 1 IS the loop
# line iff it does not start with ``[`` (after any leading ANSI escape). The
# production zones file is colorized — each anchor is wrapped as
# ``\033[38;5;244m{text}\033[0m``, so the loop line starts with the CSI escape,
# not its first letter. awk owns both the match decision and the prepend (its
# ``sprintf("%c", 27)`` is a literal escape byte across awk implementations,
# unlike grep's \x1b which only some greps interpret): it inserts the badge at
# the front of line 1, AFTER any leading ANSI escape so the badge keeps its own
# color rather than inheriting the line's dim wrap, in both colorized and
# NO_COLOR paths, and exits non-zero when line 1 is not a loop line (an overlay
# anchor, no loop currently live) so the shell falls back to a trailing badge.
if [ -n "$_loop_owner_badge" ] && [ -n "$_zones_body" ]; then
    if ! printf '%s\n' "$_zones_body" | awk -v badge="${_loop_owner_badge}${isep}" '
        function esc() { return sprintf("%c", 27) }
        NR == 1 && $0 ~ "[^[:space:]]" && $0 !~ ("^(" esc() "\\[[0-9;]*m)?\\[") {
            csi = "^" esc() "\\[[0-9;]*m"
            if (match($0, csi)) {
                lead = substr($0, 1, RLENGTH)
                printf "%s%s%s\n", lead, badge, substr($0, RLENGTH + 1)
            } else {
                printf "%s%s\n", badge, $0
            }
            prepended = 1
            next
        }
        { print }
        END { exit(prepended ? 0 : 1) }
    '; then
        printf '%s\n' "$_loop_owner_badge"
    fi
elif [ -n "$_zones_body" ]; then
    printf '%s\n' "$_zones_body"
elif [ -n "$_loop_owner_badge" ]; then
    printf '%s\n' "$_loop_owner_badge"
fi

# Chain extra statusline scripts from the DB-home `statusline_chain` setting.
# Each entry is a glob pattern; the latest match (sort -V) is run with the
# Claude stdin JSON piped in.
if [ -n "${input:-}" ]; then
    while IFS= read -r _pat; do
        [ -z "$_pat" ] && continue
        _pat="${_pat/#\~/$HOME}"
        _resolved=$(ls -d $_pat 2>/dev/null | sort -V | tail -1)
        [ -z "$_resolved" ] && continue
        case "$_resolved" in
            *.mjs|*.js) _runner="node" ;;
            *.py)       _runner="python3" ;;
            *)          _runner="bash" ;;
        esac
        printf '%s' "$input" | "$_runner" "$_resolved" 2>/dev/null
    done < <(_statusline_chain_db)
fi
