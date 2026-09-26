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
#
# LATENCY IS THE FEATURE (#135). Claude Code gives a statusline command about a
# second and renders NOTHING — not a stale bar, not a partial one — when it
# overruns. This hook computes almost nothing; its whole cost is process
# spawns, and a spawn on a box running a dozen agents costs 10-100ms, not the
# ~1ms it costs idle. So the same script that took 0.3s on a quiet machine took
# 5-8s on a loaded one and the owner's bar simply vanished. Two rules keep it
# from coming back:
#
#   * SPAWN COUNT IS THE BUDGET. One `jq` per JSON SOURCE, never one per field:
#     the payload and the saved settings share a single invocation, and
#     tick-meta.json's staleness, contributed segments and repo freshness are
#     one more. Arithmetic and string surgery are bash builtins — `awk` for a
#     percentage is a process for something `$(( ))` does for free. Before
#     adding a `jq`/`awk`/`cut`/`date`, fold it into a call that already runs.
#   * THE CHEAP LINE IS UNCONDITIONAL. Model, effort, context, rate limits, RAM,
#     CPU and disk come from stdin and three local syscalls, so they are built
#     FIRST and held in hand. Everything that touches a file, a repo, the
#     control DB or another program's script runs afterwards in `_render_tail`,
#     under a hard deadline (TEATREE_STATUSLINE_BUDGET, default 0.7s). If that
#     tail overruns or dies, the cheap line still renders, with a marker saying
#     the rest was dropped. Partial output beats the blank bar.

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
input=""
# ONE jq for the whole payload. This used to be six `jq` calls plus three `cut`
# calls, and a seventh `jq` further down for the saved `/effort` default — ten
# processes spent before the bar had a single character on it. The settings file
# is slurped into the SAME invocation with `--rawfile` and parsed inside jq, so
# a malformed settings.json degrades to "no effort" via `fromjson?` instead of
# failing the payload parse and taking model/ctx/usage down with it.
# stdin is drained with the `read` builtin: `input=$(cat)` is a fork and an exec
# for bytes bash can read itself.
if ! [ -t 0 ] && command -v jq >/dev/null 2>&1; then
    _line=""
    while IFS= read -r _line || [ -n "$_line" ]; do
        input="${input}${_line}"$'\n'
    done
    if [ -n "$input" ]; then
        # /dev/null stands in for an absent settings file so jq still gets its
        # --rawfile operand; `fromjson?` then yields no value and `// {}` covers it.
        _effort_cfg="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
        [ -r "$_effort_cfg" ] || _effort_cfg=/dev/null
        IFS=$'\037' read -r session_id model effort ctx_pct \
            five_hour_pct five_hour_resets_at seven_day_pct seven_day_resets_at <<EOF
$(printf '%s' "$input" | jq -r --rawfile _cfgraw "$_effort_cfg" '
    # The `cut -d. -f1` these fields used to be piped through: a percentage is
    # rendered as a whole number.
    def whole: if . == null then "" else (tostring | split(".")[0]) end;
    (($_cfgraw | fromjson?) // {}) as $cfg
    # The session `/effort` level. The harness statusline payload does not
    # currently carry it, but it is read here first so the segment upgrades for
    # free if a future payload exposes `.effort` (or `.model.effort`); otherwise
    # the saved settings default answers.
    | ((.effort // .model.effort) as $e
        | if ($e | type) == "object" then ($e.level // "")
          elif ($e | type) == "string" then $e
          else "" end) as $live_effort
    | [ (.session_id // ""),
        (.model.display_name // ""),
        (if $live_effort != "" then $live_effort
          else (($cfg.effortLevel // "") | if type == "string" then . else "" end) end),
        (.context_window.used_percentage | whole),
        (.rate_limits.five_hour.used_percentage | whole),
        (.rate_limits.five_hour.resets_at // ""),
        (.rate_limits.seven_day.used_percentage | whole),
        (.rate_limits.seven_day.resets_at // "") ]
    | map(tostring | gsub("[\\n\\r\\t]"; " ") | gsub("\u001f"; " ")) | join("\u001f")' 2>/dev/null)
EOF
    fi
fi
# The render gate is primarily the `autoload` owner flag (below), NOT the per-session
# `.teatree-active` marker as a hard requirement. That marker is written by
# SessionStart-engage / a teatree-skill load, but the harness runs the loop in a
# background `bg-spare` daemon session (which gets the marker and owns the tick) while
# the owner's foreground TUI sessions frequently never get it — so ANDing the marker
# with autoload blanked the statusline in exactly the sessions the owner looks at.
# `autoload` is the ONE owner flag that "engages the session", so it is the PRIMARY
# gate here. A secondary opt-in — `statusline_engaged_render` (#3502, default false) —
# ADDITIONALLY renders in a session the owner explicitly engaged by hand (an engage
# marker present) even with autoload off. Loop *arming* keeps its stricter
# `marker AND autoload` gate (hook_router._loop_auto_load_active); this is display
# *visibility*. The #256 colleague guarantee still holds: with `autoload` off and the
# opt-in unset the bar shows only the neutral hint, regardless of the marker.

# The host-visible projection of the container-owned control DB (#3499). Every
# ConfigSetting read below resolves a HOST sqlite path, but a containerized deploy
# keeps the control DB in a volume the host cannot open at all, so that path is
# absent (or, after the migration that moved it, a leftover 0-byte stub) and each
# reader returns empty — which reads as "autoload is off" and silently takes the
# whole statusline with it. The publisher writes this file for exactly this
# consumer; `teatree.config.cold_db.canonical_projection()` is the Python half of
# the same fallback.
#
# Echoes the decoded value — empty when the key is simply unset — and returns:
#   0  the projection ANSWERED (a value, or a readable projection with no such key)
#   1  there is no projection on this host to answer with
#   2  a projection is present but could not be read or parsed
# The 1/2 split is the whole point: "no store here" is a plain-clone host that has
# genuinely opted into nothing, while "a store is here and I cannot read it" is an
# unknown that must never be rendered as a stored value.
_projection_setting() {
    command -v jq >/dev/null 2>&1 || return 1
    local proj="${TEATREE_HOST_PROJECTION:-${XDG_DATA_HOME:-$HOME/.local/share}/teatree/host-projection.json}"
    if [ ! -r "$proj" ]; then
        # Present yet denied is the unreadable half of the 1/2 split, not an absence (#4205).
        [ -e "$proj" ] && return 2
        return 1
    fi
    # `settings` is keyed by SCOPE; "" is global. Values are stored decoded, so a
    # bool arrives as `true`/`false` — the same text the sqlite read yields.
    local out
    out=$(jq -r --arg k "$1" \
        'if (.settings | type) != "object" then halt_error(3) else (.settings[""][$k] // empty) end' \
        "$proj" 2>/dev/null) || return 2
    printf '%s' "$out"
}

# THE single GLOBAL-scope ConfigSetting read for every key this script gates on, so
# the keys can never disagree about what "could not be read" means. Read-only via
# the sqlite3 CLI (the statusline needs no importable teatree python), mirroring
# teatree.config.cold_reader's WAL fallback: `mode=ro` first (live writer, sidecars
# present), then `immutable=1` (quiescent WAL, no sidecars — `mode=ro` then errors),
# then the published projection.
#
# Echoes the JSON-decoded value and returns 0 when a tier ANSWERED; empty output
# there means the store is readable and holds no such row, a genuine absence the
# caller's default covers. Returns 1 — and echoes nothing — when a store is present
# on this host but NO tier could read it. That is `unknown`, not `unset`; conflating
# them is what rendered a confident "off" for a setting the DB actually holds as
# true (#4041). The DB is tested with `-s`, not `-f`: the control-DB migration left
# a 0-byte stub at the old host path, and a stub that holds nothing is a store this
# host cannot see — exactly the case the projection exists to answer. Presence is
# then re-tested with `-e` at the end (#4205): the stub, and a non-empty DB on a
# host with no sqlite3, both reach the projection, and when THAT cannot answer
# either, a store is here that nothing read — the same unknown, not a fresh clone.
_config_setting_read() {
    local key="$1" db out rc
    db="${T3_CONFIG_DB:-${XDG_DATA_HOME:-$HOME/.local/share}/teatree/db.sqlite3}"
    if command -v sqlite3 >/dev/null 2>&1 && [ -s "$db" ]; then
        local q="SELECT value FROM teatree_config_setting WHERE scope='' AND key='${key}' LIMIT 1;"
        if out=$(sqlite3 "file:${db}?mode=ro" "$q" 2>/dev/null) \
            || out=$(sqlite3 "file:${db}?immutable=1" "$q" 2>/dev/null); then
            printf '%s' "$out"
            return 0
        fi
        # A DB file present yet unreadable is a store this host cannot see, exactly
        # like the container-only volume — fall through rather than report its
        # contents as empty.
        _projection_setting "$key" && return 0
        return 1
    fi
    _projection_setting "$key"
    rc=$?
    case "$rc" in
        0) return 0 ;;
        2) return 1 ;;
    esac
    # A store IS on this host (the 0-byte stub, or a DB no sqlite3 exists to open)
    # and no tier read it — unknown, exactly as in the sqlite branch above.
    [ -e "$db" ] && return 1
    # No store on this host at all: nothing was ever written here to read, so the
    # shipped default is the answer rather than a guess. This keeps the #256
    # colleague guarantee — a plain clone renders the neutral off hint, not a "?".
    return 0
}

# The canonical ConfigSetting store's GLOBAL `statusline_chain` (a JSON array of
# glob patterns), one element per line. Same tiers as `_config_setting_read`, kept
# separate because the caller wants one pattern per line and the projection holds
# the array whole. Fail-silent-empty is correct HERE — an unresolved chain renders
# no extra chips, which is a display absence, not a verdict about owner intent.
_statusline_chain_db() {
    local db out
    db="${T3_CONFIG_DB:-${XDG_DATA_HOME:-$HOME/.local/share}/teatree/db.sqlite3}"
    if command -v sqlite3 >/dev/null 2>&1 && [ -s "$db" ]; then
        local q="SELECT je.value FROM teatree_config_setting t, json_each(t.value) je WHERE t.scope='' AND t.key='statusline_chain';"
        if out=$(sqlite3 "file:${db}?mode=ro" "$q" 2>/dev/null) \
            || out=$(sqlite3 "file:${db}?immutable=1" "$q" 2>/dev/null); then
            [ -n "$out" ] && printf '%s\n' "$out"
            return 0
        fi
    fi
    command -v jq >/dev/null 2>&1 || return 0
    local proj="${TEATREE_HOST_PROJECTION:-${XDG_DATA_HOME:-$HOME/.local/share}/teatree/host-projection.json}"
    [ -r "$proj" ] || return 0
    jq -r '.settings[""].statusline_chain // [] | .[]' "$proj" 2>/dev/null || return 0
}

# Session-start loop/statusline auto-load is OPT-IN (#256): default OFF so a
# colleague who merely clones the repo never sees the loop statusline. ``autoload``
# is the ONE owner flag (it engages the session AND arms its loops). Mirrors
# hook_router._autoload_enabled — env T3_AUTOLOAD first, then the canonical
# ConfigSetting read (_config_setting_read). autoload is DB-home only (no file
# fallback).
#
# Echoes exactly one of `on`, `off`, `unknown`. `unknown` is NOT `off`: it means a
# store exists here that no tier could read, so the shipped default is a guess and
# must not be rendered as the owner's stored choice.
autoload_state() {
    local env_val="${T3_AUTOLOAD:-}"
    if [ -n "$env_val" ]; then
        case "$(printf '%s' "$env_val" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
            1|true|yes|on) printf 'on' ;;
            *) printf 'off' ;;
        esac
        return 0
    fi
    # The exit status of the substitution is the reader's, so the unknown signal
    # survives the subshell that a plain global would not.
    local value
    value=$(_config_setting_read autoload) || { printf 'unknown'; return 0; }
    case "$value" in
        true) printf 'on' ;;
        *) printf 'off' ;;
    esac
}

# A session is explicitly engaged when it carries either engage marker under
# `state_dir`: `.teatree-active` (SessionStart-engage / a teatree-requiring skill)
# or `.t3-engaged` (any `t3:` skill loaded).
session_engaged() {
    [ -n "$1" ] && { [ -f "$state_dir/$1.teatree-active" ] || [ -f "$state_dir/$1.t3-engaged" ]; }
}

# The opt-in render path (`statusline_engaged_render`, #3502): the flag is a
# strict-bool `true` AND this session is explicitly engaged. Default false → a
# colleague never reaches it (#256). An unreadable store is not `true`, so this
# stays closed; the honesty of that state is carried by `autoload_state` instead,
# which is the flag the hint line is about.
engaged_render_enabled() {
    local value
    value=$(_config_setting_read statusline_engaged_render) || return 1
    case "$value" in
        true) session_engaged "$1" ;;
        *) return 1 ;;
    esac
}

teatree_autoload_state=$(autoload_state)
if [ -n "$session_id" ] && [ "$teatree_autoload_state" != "on" ] && ! engaged_render_enabled "$session_id"; then
    # #3233: CC discards zero-byte statusline output, so a silent ``exit 0``
    # here renders a mysteriously BLANK bar under the non-TTY CC invocation
    # (session_id set) — invisible to every run-the-script-by-hand debug pass.
    # Emit one neutral hint line instead so the bar is never empty; the #256
    # colleague guarantee still holds (the loop statusline stays suppressed —
    # only this one-line how-to shows).
    #
    # #4041: "I could not read the setting" and "the setting is off" are different
    # facts, and the second is a verdict the reader has no way to distrust. The
    # degraded read gets its own line saying so, and points at the check that
    # explains it — never the enable-it hint, which would be advice to overwrite a
    # value that may already be true.
    if [ "$teatree_autoload_state" = "unknown" ]; then
        printf 'teatree: statusline ? · autoload UNKNOWN — config unreadable here, NOT off · diagnose: t3 doctor check\n'
    else
        printf 'teatree: statusline off (autoload disabled) · enable: t3 <overlay> config_setting set autoload true\n'
    fi
    exit 0
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

# Both of these used to `printf` their result for a `$(…)` caller to capture,
# which forks a subshell per call — five for the percentages alone. They set a
# variable instead: same output, no processes.
_cp=""
color_pct() {
    local pct="$1"
    if (( pct >= 95 )); then _cp="${_RED}${pct}%${_RST}"
    elif (( pct >= 80 )); then _cp="${_YLW}${pct}%${_RST}"
    else _cp="${_GRN}${pct}%${_RST}"
    fi
}

_frt=""
format_reset_time() {
    local resets_at="$1"
    _frt=""
    [ -z "$resets_at" ] || [ "$resets_at" = "empty" ] && return
    local reset_time=""
    if [[ "$resets_at" =~ ^[0-9]+$ ]]; then
        if [[ "$OSTYPE" == "darwin"* ]]; then
            reset_time=$(date -j -r "$resets_at" "+%H:%M" 2>/dev/null)
        else
            reset_time=$(date -d "@$resets_at" "+%H:%M" 2>/dev/null)
        fi
    fi
    [ -n "$reset_time" ] && _frt=" ${_DIM}(until ${reset_time})${_RST}"
    return 0
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
g_updates=""
g_resource=""
g_team=""
g_segend=""

# ─── The cheap line ────────────────────────────────────────────────────────
# Everything from here to `_cheap_header` is stdin plus three local syscalls,
# and it is built BEFORE anything that reads a file, a repo or the control DB
# so it survives every one of those being dead or slow (see `_render_tail`).

# RAM, CPU and disk. This block used to spend thirteen processes — three
# `sysctl` calls, `vm_stat`, `df` and eight `awk` invocations whose entire job
# was dividing two integers and printing one decimal place. It now spends
# three: the `sysctl` names are fetched in one call, `vm_stat` and `df` are
# parsed with the `read` builtin, and every percentage and GB figure is
# `$(( ))` arithmetic scaled by ten (`awk`'s "%.1f"/"%.0f" rounding reproduced
# by adding half a unit before the integer divide).
# NOTE(#962): the RAM computation is slated to move into `teatree.system.memory`
# (`t3 tool memory --json`) as the single source consumed by both the statusline
# and a provision-path RAM auto-throttle. See souliane/teatree#962.
_ram_segment=""
_cpu_segment=""
_disk_segment=""

_ram_total=""
_ncpu="${TEATREE_STATUSLINE_NCPU:-}"
_loadavg_raw=""
if [[ "$OSTYPE" == "darwin"* ]]; then
    # One call, four keys — `vm.loadavg` arrives as `{ 1.23 4.56 7.89 }`.
    # `vm.swapusage` is the host-only signal Docker Desktop cannot report.
    _sysctl_l1=""; _sysctl_l2=""; _sysctl_l3=""; _sysctl_l4=""
    { read -r _sysctl_l1; read -r _sysctl_l2; read -r _sysctl_l3; read -r _sysctl_l4; } <<EOF
$(sysctl -n hw.memsize hw.ncpu vm.loadavg vm.swapusage 2>/dev/null)
EOF
    _ram_total="$_sysctl_l1"
    [ -z "$_ncpu" ] && _ncpu="$_sysctl_l2"
    _loadavg_raw="${_sysctl_l3#*\{ }"
    _loadavg_raw="${_loadavg_raw%% *}"
fi

if [ -n "$_ram_total" ]; then
    _page_sz=""; _free=""; _inact=""
    _vml=""
    while IFS= read -r _vml; do
        case "$_vml" in
            *"page size of "*)
                _page_sz="${_vml#*page size of }"
                _page_sz="${_page_sz%% *}"
                ;;
            "Pages free:"*)
                _free="${_vml##*[[:space:]]}"; _free="${_free%.}"
                ;;
            "Pages inactive:"*)
                _inact="${_vml##*[[:space:]]}"; _inact="${_inact%.}"
                ;;
        esac
    done <<EOF
$(vm_stat 2>/dev/null)
EOF
    if [[ "$_page_sz" =~ ^[0-9]+$ ]] && [[ "$_free" =~ ^[0-9]+$ ]] && [[ "$_inact" =~ ^[0-9]+$ ]]; then
        _ram_used=$(( _ram_total - (_free + _inact) * _page_sz ))
        _ram_pct=$(( _ram_used * 100 / _ram_total ))
        _used_tenths=$(( (_ram_used * 10 + 536870912) / 1073741824 ))
        _total_gb=$(( (_ram_total + 536870912) / 1073741824 ))
        color_pct "$_ram_pct"
        _ram_segment="${_LBL}ram=${_RST}${_cp}${_LBL} $(( _used_tenths / 10 )).$(( _used_tenths % 10 ))/${_total_gb}G${_RST}"
        # Publish a fresh, host-scoped reading to the data dir Docker bind-mounts.
        # Read the first JSON line with a shell builtin to throttle writes to 15s;
        # the statusline hot path gains no parser process and only one `mv` on a
        # write. A stale/missing feed is explicitly reported by the governor.
        _swap_total="${_sysctl_l4#*total = }"
        _swap_total="${_swap_total%%M*}"
        _swap_used="${_sysctl_l4#*used = }"
        _swap_used="${_swap_used%%M*}"
        if [[ "$_ncpu" =~ ^[0-9]+$ ]] && [[ "$_loadavg_raw" =~ ^[0-9]+\.?[0-9]*$ ]] \
            && [[ "$_swap_total" =~ ^[0-9]+\.?[0-9]*$ ]] && [[ "$_swap_used" =~ ^[0-9]+\.?[0-9]*$ ]]; then
            _host_pressure_dir="${T3_LOOP_REGISTRY_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/teatree}"
            _host_pressure_file="${_host_pressure_dir}/host-pressure.json"
            _host_epoch=$(date +%s)
            _last_host_epoch=0
            if [ -r "$_host_pressure_file" ]; then
                IFS= read -r _old_host_json < "$_host_pressure_file"
                if [[ "$_old_host_json" =~ \"epoch\":([0-9]+) ]]; then _last_host_epoch="${BASH_REMATCH[1]}"; fi
            fi
            if [ $(( _host_epoch - _last_host_epoch )) -ge 15 ]; then
                _host_ram_mib=$(( (_free + _inact) * _page_sz / 1048576 ))
                _swap_total_mib="${_swap_total%%.*}"
                _swap_used_mib="${_swap_used%%.*}"
                if [ -d "$_host_pressure_dir" ] || mkdir -p "$_host_pressure_dir" 2>/dev/null; then
                    _host_pressure_tmp="${_host_pressure_file}.$$"
                    if printf '{"epoch":%s,"cores":%s,"load1":%s,"ram_available_mib":%s,"swap_used_mib":%s,"swap_total_mib":%s}\n' \
                        "$_host_epoch" "$_ncpu" "$_loadavg_raw" "$_host_ram_mib" "$_swap_used_mib" "$_swap_total_mib" \
                        >"$_host_pressure_tmp" 2>/dev/null; then
                        mv -f "$_host_pressure_tmp" "$_host_pressure_file" 2>/dev/null || true
                    fi
                fi
            fi
        fi
    fi
elif [ -r /proc/meminfo ]; then
    # Linux: /proc is read with builtins, so this branch spends no processes at all.
    _ram_total_kb=""; _ram_avail_kb=""
    _ml=""
    while IFS= read -r _ml; do
        case "$_ml" in
            MemTotal:*)     _ram_total_kb="${_ml//[!0-9]/}" ;;
            MemAvailable:*) _ram_avail_kb="${_ml//[!0-9]/}" ;;
        esac
    done < /proc/meminfo
    if [[ "$_ram_total_kb" =~ ^[0-9]+$ ]] && [[ "$_ram_avail_kb" =~ ^[0-9]+$ ]] && [ "$_ram_total_kb" -gt 0 ]; then
        _ram_used_kb=$(( _ram_total_kb - _ram_avail_kb ))
        _ram_pct=$(( _ram_used_kb * 100 / _ram_total_kb ))
        _used_tenths=$(( (_ram_used_kb * 10 + 524288) / 1048576 ))
        _total_gb=$(( (_ram_total_kb + 524288) / 1048576 ))
        color_pct "$_ram_pct"
        _ram_segment="${_LBL}ram=${_RST}${_cp}${_LBL} $(( _used_tenths / 10 )).$(( _used_tenths % 10 ))/${_total_gb}G${_RST}"
    fi
fi

# Free disk space on the volume holding $HOME (cross-platform via POSIX df).
# Colored by used% so it goes red as the disk fills, mirroring the RAM segment.
# `-P` guarantees one unwrapped record, so the `read` builtin can take it apart.
_df_hdr=""; _df_fs=""; _df_blocks=""; _df_used=""; _df_avail=""; _df_cap=""; _df_rest=""
{ read -r _df_hdr; read -r _df_fs _df_blocks _df_used _df_avail _df_cap _df_rest; } <<EOF
$(df -Pk "$HOME" 2>/dev/null)
EOF
_disk_used_pct="${_df_cap%\%}"
if [[ "$_df_avail" =~ ^[0-9]+$ ]] && [[ "$_disk_used_pct" =~ ^[0-9]+$ ]]; then
    color_pct "$_disk_used_pct"
    _disk_segment="${_LBL}disk=${_RST}${_cp}${_LBL} $(( (_df_avail + 524288) / 1048576 ))G free${_RST}"
fi

# CPU load (macOS/Linux). A single non-delayed read of the 1-minute load
# average — never a multi-second sampler like `top -l 2`, so the hook stays
# fast. The load is normalized by core count and rendered as a percent so it
# reads alongside the RAM/disk indicators under the same color thresholds;
# above 100% means more runnable work than cores, which color_pct paints red.
if [ -n "${TEATREE_STATUSLINE_LOADAVG_FILE:-}" ]; then
    _loadavg_raw=""
    [ -r "$TEATREE_STATUSLINE_LOADAVG_FILE" ] && read -r _loadavg_raw _df_rest < "$TEATREE_STATUSLINE_LOADAVG_FILE"
elif [ -z "$_loadavg_raw" ] && [ -r /proc/loadavg ]; then
    read -r _loadavg_raw _df_rest < /proc/loadavg
fi
if [ -z "$_ncpu" ] && [[ "$OSTYPE" != "darwin"* ]]; then
    _ncpu=$(nproc 2>/dev/null)
fi
if [[ "$_loadavg_raw" =~ ^[0-9]+\.?[0-9]*$ ]] && [[ "$_ncpu" =~ ^[0-9]+$ ]] && [ "$_ncpu" -gt 0 ]; then
    # load × 100 ÷ cores, in integer arithmetic: the load is read as hundredths
    # ("4.00" → 400) so the percentage is just hundredths ÷ cores, rounded.
    _l_int="${_loadavg_raw%%.*}"
    _l_frac="${_loadavg_raw#*.}"
    [ "$_l_frac" = "$_loadavg_raw" ] && _l_frac=""
    _l_frac="${_l_frac}00"
    _load_centi=$(( 10#${_l_int:-0} * 100 + 10#${_l_frac:0:2} ))
    _cpu_pct=$(( (2 * _load_centi + _ncpu) / (2 * _ncpu) ))
    color_pct "$_cpu_pct"
    _cpu_segment="${_LBL}cpu=${_RST}${_cp}"
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

# Effort level (`/effort`): the live payload field, else the saved settings
# default — both already resolved by the single payload `jq` above. Rendered as
# a short `· <effort>` suffix on the model chunk (e.g. `model=opus-4-8 · medium`);
# omitted entirely when unknown so the segment stays honest and leaves no
# dangling separator.
if [ -n "$model" ]; then
    g_context="${_LBL}model=${_RST}${_GRN}${model}${_RST}"
    if [ -n "$effort" ]; then
        g_context="${g_context}${_LBL} · ${_RST}${_GRN}${effort}${_RST}"
    fi
fi
if [ -n "$ctx_pct" ] && [ "$ctx_pct" != "empty" ]; then
    [ -n "$g_context" ] && g_context="${g_context}${isep}"
    color_pct "$ctx_pct"
    g_context="${g_context}${_LBL}ctx=${_RST}${_cp}"
fi
# The per-session t3-master badge does NOT go in g_context — it is
# loop-specific info and belongs on the loop line region (appended after the
# cat'd zones file below), so all loop state has one visual home.
if [ -n "$five_hour_pct" ] && [ "$five_hour_pct" != "empty" ]; then
    color_pct "$five_hour_pct"
    format_reset_time "$five_hour_resets_at"
    g_usage="${_LBL}5h=${_RST}${_cp}${_frt}"
fi
if [ -n "$seven_day_pct" ] && [ "$seven_day_pct" != "empty" ]; then
    [ -n "$g_usage" ] && g_usage="${g_usage}${isep}"
    color_pct "$seven_day_pct"
    g_usage="${g_usage}${_LBL}7d=${_RST}${_cp}"
fi

# The guaranteed line: what renders when every expensive source is dead. The
# enriched header `_render_tail` builds is this plus the freshness, contributed,
# mates, TODO and skills groups, joined the same way.
# Sets `_joined` rather than echoing it: a `$(…)` here would fork a subshell for
# a string concatenation.
_joined=""
_join_groups() {
    local part
    _joined=""
    for part in "$@"; do
        [ -z "$part" ] && continue
        if [ -z "$_joined" ]; then _joined="$part"; else _joined="${_joined}${gsep}${part}"; fi
    done
}
_join_groups "$g_context" "$g_usage" "$g_resource"
_cheap_header="$_joined"
# Claude Code's statusline docs warn that long, multi-line ANSI output "may get
# truncated or wrap awkwardly" and that "multi-line status lines with escape
# codes are more prone to rendering issues than single-line plain text" — on some
# render surfaces a ~900-char multi-line loop line makes the WHOLE bar render
# blank. So every emitted line (header, zones, owner badge, chain-script output)
# is bounded to the visible terminal width at one choke point below. The width is
# COLUMNS when set and > 0; else `tput cols` when stdout is a real terminal (a
# piped, non-terminal stdout — Claude's own capture, a test — has no meaningful
# terminal width, so it is not consulted); else a safe 200. `_cap_line_widths`
# is an ANSI-aware awk one-pass filter (below).
_cap_cols="${COLUMNS:-}"
if ! [[ "$_cap_cols" =~ ^[0-9]+$ ]] || [ "$_cap_cols" -le 0 ]; then
    _cap_cols=""
    if [ -t 1 ] && command -v tput >/dev/null 2>&1; then
        _cap_cols=$(tput cols 2>/dev/null)
    fi
fi
if ! [[ "$_cap_cols" =~ ^[0-9]+$ ]] || [ "$_cap_cols" -le 0 ]; then
    _cap_cols=200
fi

# ANSI-aware per-line width cap. Measures ONLY visible characters — SGR
# (`\033[…m` / any CSI) and OSC 8 hyperlink wrappers (`\033]8;…\033\\` / BEL) are
# copied verbatim but counted as zero width — so a line already within width
# passes through byte-for-byte (escapes and OSC 8 links intact) and a too-long
# line is cut on a character boundary (never mid-escape), marked with a single
# `…` ellipsis, and terminated with a reset so colour never bleeds past the cut.
# One awk process for the whole stream keeps the hook fast (<10ms).
#
# Scoped to `LC_ALL=C` (byte mode) so it never calls `towc`: macOS's onetrueawk
# aborts with a `towc: multibyte conversion failure` on the statusline's own
# multibyte furniture (`·` `│` `⚠` `—` `…`), and that abort blanks the WHOLE bar
# (souliane/teatree#3286). In byte mode visible-width counting is byte-based (a
# multibyte glyph counts as its UTF-8 byte length — harmless for a trim-only
# cap), the ASCII-only escape regexes are unaffected, and multibyte content
# passes through unchanged. Scoped to this one invocation so date/sort elsewhere
# keep their locale.
_cap_line_widths() {
    LC_ALL=C awk -v cap="$_cap_cols" '
    function viswidth(s,   n, i, rest, vis) {
        n = length(s); i = 1; vis = 0
        while (i <= n) {
            rest = substr(s, i)
            if (match(rest, /^\033\[[0-9;?]*[ -\/]*[@-~]/)) { i += RLENGTH; continue }
            if (match(rest, /^\033\][^\033\007]*(\033\\|\007)/)) { i += RLENGTH; continue }
            if (match(rest, /^\033./)) { i += RLENGTH; continue }
            # Byte-mode (LC_ALL=C) UTF-8 grouping: a lead byte and its
            # continuation bytes count as ONE visible glyph, so a multibyte
            # separator/ellipsis is width 1 rather than its byte length.
            if (match(rest, /^[\300-\377][\200-\277]*/)) { vis++; i += RLENGTH; continue }
            vis++; i++
        }
        return vis
    }
    function capline(s, limit,   n, i, rest, out, vis) {
        n = length(s); i = 1; out = ""; vis = 0
        while (i <= n) {
            rest = substr(s, i)
            if (match(rest, /^\033\[[0-9;?]*[ -\/]*[@-~]/)) { out = out substr(rest, 1, RLENGTH); i += RLENGTH; continue }
            if (match(rest, /^\033\][^\033\007]*(\033\\|\007)/)) { out = out substr(rest, 1, RLENGTH); i += RLENGTH; continue }
            if (match(rest, /^\033./)) { out = out substr(rest, 1, RLENGTH); i += RLENGTH; continue }
            if (vis >= limit) break
            # Emit a whole UTF-8 sequence so the cut never bisects a glyph.
            if (match(rest, /^[\300-\377][\200-\277]*/)) { out = out substr(rest, 1, RLENGTH); vis++; i += RLENGTH; continue }
            out = out substr(rest, 1, 1); vis++; i++
        }
        return out "\342\200\246" "\033[0m"
    }
    { if (viswidth($0) <= cap) print $0; else print capline($0, cap - 1) }
    '
}


# ─── The enriched tail ─────────────────────────────────────────────────────
# Everything below reads a file, a git repo, the control DB or somebody else's
# script — the parts that can be slow, stale or hung. It runs as ONE bounded
# unit (see the dispatch at the bottom): it renders the whole bar when it
# finishes in time, and when it does not, the cheap line above renders without
# it. Nothing in here can blank the bar.
_render_tail() {

skills=""
todos_done=""
todos_total=""
todos_wip=""
if [ -n "$session_id" ]; then
    skills_file="$state_dir/${session_id}.skills"
    if [ -r "$skills_file" ]; then
        # `paste -sd ' '` for a join two builtins do.
        _sk_line=""
        while IFS= read -r _sk_line || [ -n "$_sk_line" ]; do
            [ -z "$_sk_line" ] && continue
            skills="${skills}${skills:+ }${_sk_line}"
        done < "$skills_file"
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
        IFS=' ' read -r _total todos_done todos_wip <<EOF
$(jq -rs '
            map(select(type == "object") | .status // "pending") as $s
            | "\($s | length) \($s | map(select(. == "completed")) | length) \($s | map(select(. == "in_progress")) | length)"
        ' "$tasks_dir"/*.json 2>/dev/null || true)
EOF
        _total=${_total:-0}
        if [ "$_total" -gt 0 ] 2>/dev/null; then
            todos_total="$_total"
            todos_done="${todos_done:-0}"
            todos_wip="${todos_wip:-0}"
        else
            todos_done=""; todos_wip=""
        fi
    fi
fi

# ONE jq for tick-meta.json, which three separate readers used to open: the
# staleness probe (2 calls), the contributed segments (1) and repo freshness
# (1 for the object plus THREE MORE PER REPO for its fields). It emits a tagged
# TSV stream — `M` meta, `S` segment, `F` freshness row — that one builtin loop
# takes apart. `dirname` is `${var%/*}`.
_tick_meta="${target%.txt}-meta.json"
[ ! -r "$_tick_meta" ] && _tick_meta="${target%/*}/tick-meta.json"

_rendered_at=""
_sl_cadence=""
_seg_usage=""
_seg_header=""
_seg_end=""
_fresh_rows=""
if [ -r "$_tick_meta" ] && command -v jq >/dev/null 2>&1; then
    _tm_kind=""; _tm_a=""; _tm_b=""; _tm_c=""; _tm_d=""
    while IFS=$'\037' read -r _tm_kind _tm_a _tm_b _tm_c _tm_d; do
        case "$_tm_kind" in
            M)
                _rendered_at="$_tm_a"
                _sl_cadence="$_tm_b"
                ;;
            S)
                [ -z "$_tm_c" ] && continue
                case "$_tm_b" in
                    green)  _sc="$_GRN" ;;
                    yellow) _sc="$_YLW" ;;
                    red)    _sc="$_RED" ;;
                    *)      _sc="$_BLU" ;;
                esac
                _rendered="${_sc}${_tm_c}${_RST}"
                case "$_tm_a" in
                    usage)  [ -n "$_seg_usage" ]  && _seg_usage="${_seg_usage}${isep}";   _seg_usage="${_seg_usage}${_rendered}" ;;
                    header) [ -n "$_seg_header" ] && _seg_header="${_seg_header}${isep}"; _seg_header="${_seg_header}${_rendered}" ;;
                    *)      [ -n "$_seg_end" ]    && _seg_end="${_seg_end}${isep}";       _seg_end="${_seg_end}${_rendered}" ;;
                esac
                ;;
            F)
                _fresh_rows="${_fresh_rows}${_tm_a}"$'\037'"${_tm_b}"$'\037'"${_tm_c}"$'\037'"${_tm_d}"$'\n'
                ;;
        esac
    done <<EOF
$(jq -r '
    (["M", ((.rendered_at // "") | tostring), ((.cadence // "") | tostring)] | map(tostring | gsub("[\\n\\r\\t]"; " ") | gsub("\u001f"; " ")) | join("\u001f")),
    # Contributed inline segments (souliane/teatree#3237). Loops/overlays generate
    # named segments (id/text/color/placement); core assembles them here. Each is
    # computed Python-side at tick cadence and handed over in this sidecar
    # ``segments`` list; this hook only colors and places them. Placement anchors:
    #   usage  → the usage group (where the SDK cost chip sits — it is the first,
    #            core-produced ``usage`` segment, the retired dedicated ``cost_chip``
    #            key generalized into this one mechanism)
    #   header → next to the repo-freshness segments (the updates group)
    #   after:<id> → resolved here to the placement of the segment it names, so
    #                it lands in that group right after the segment it follows
    # An unknown/dangling placement degrades to end-of-line, never an error.
    ((.segments // []) as $segs
      | $segs[]
      | . as $s
      | (($s.placement // "header")) as $p
      | (if ($p | startswith("after:"))
        then (($p[6:]) as $ref
              | ([$segs[] | select((.id // "") == $ref) | (.placement // "header")] | .[0] // "end"))
        else $p end) as $resolved
      | ["S", $resolved, ($s.color // "-"), ($s.text // "")] | map(tostring | gsub("[\\n\\r\\t]"; " ") | gsub("\u001f"; " ")) | join("\u001f")),
    # Repo freshness, sorted by repo name so the chips keep the order the
    # per-key `jq keys[]` loop rendered them in.
    ((.freshness // {}) | to_entries | sort_by(.key) | .[]
      | ["F", .key, ((.value.behind? // -1) | tostring), ((.value.fetch_epoch? // 0) | tostring), (.value.path? // "")] | map(tostring | gsub("[\\n\\r\\t]"; " ") | gsub("\u001f"; " ")) | join("\u001f"))
' "$_tick_meta" 2>/dev/null)
EOF
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
_now=""
if [[ "$_rendered_at" =~ ^[0-9]+$ ]]; then
    [[ "$_sl_cadence" =~ ^[0-9]+$ ]] || _sl_cadence=720
    _sl_cutoff=$(( 2 * _sl_cadence ))
    [ "$_sl_cutoff" -lt 300 ] && _sl_cutoff=300
    _now=$(date +%s)
    _sl_age=$(( _now - _rendered_at ))
    if [ "$_sl_age" -gt "$_sl_cutoff" ] 2>/dev/null; then
        if (( _sl_age < 3600 )); then _sl_age_h="$(( _sl_age / 60 ))m"
        elif (( _sl_age < 86400 )); then _sl_age_h="$(( _sl_age / 3600 ))h"
        else _sl_age_h="$(( _sl_age / 86400 ))d"
        fi
        _stale_banner=$'\033[1;31m'"⚠ statusline STALE — last rendered ${_sl_age_h} ago; loop may be stopped (re-register its /loop via /t3:loops, or run \`t3 loops tick\`)"$'\033[0m'
    fi
fi

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

if [ -n "$_seg_usage" ]; then
    [ -n "$g_usage" ] && g_usage="${g_usage}${isep}"
    g_usage="${g_usage}${_seg_usage}"
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

# Repo freshness from the `F` rows parsed above. The next-tick countdown that
# used to live here is gone (#130): tick timing belongs on the single dedicated
# loop line, not split between this header and the loop line.
_freshness_segment=""
if [ -n "$_fresh_rows" ]; then
    [ -z "$_now" ] && _now=$(date +%s)
    _fresh_parts=""
    while IFS=$'\037' read -r _repo _behind _fetch_ep _path; do
        [ -z "$_repo" ] && continue
        # Recompute behind inline when FETCH_HEAD has been touched since the
        # tick wrote this entry (e.g. a manual `git pull` in another terminal).
        # Cheap: one local `git rev-list` per repo, no network — but "local" is
        # not "bounded", and a repo on a stalled disk would take the whole tail
        # with it, so the recompute gets its own short deadline and the cached
        # value stands when it overruns.
        if [ -n "$_path" ] && [ -f "$_path/.git/FETCH_HEAD" ]; then
            # Linux stat (-c) first, BSD/macOS (-f) fallback. The reverse
            # order silently produces wrong output on Linux because
            # `stat -f` exists there too with a different meaning.
            _disk_ep=$(stat -c %Y "$_path/.git/FETCH_HEAD" 2>/dev/null || stat -f %m "$_path/.git/FETCH_HEAD" 2>/dev/null || echo 0)
            if [ "$_disk_ep" -gt "$_fetch_ep" ] 2>/dev/null; then
                _fresh_behind=""
                if _run_bounded "$_git_budget" "${_tmp_prefix}.git" git -C "$_path" rev-list HEAD..origin/main --count; then
                    read -r _fresh_behind < "${_tmp_prefix}.git" || _fresh_behind=""
                fi
                if [[ "$_fresh_behind" =~ ^[0-9]+$ ]]; then
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
            # This is git HEAD..origin/main, NOT factory queue depth. Name it so
            # a long-lived fork divergence cannot be misread as 2,688 queued jobs.
            _label="${_fc}${_repo}${_RST}${_LBL} git-behind=${_RST}${_fc}${_behind}${_RST}"
            [ -n "$_age" ] && _label="${_label}${_LBL}(${_age})${_RST}"
        elif [ -n "$_age" ]; then
            _label="${_LBL}${_repo}=${_age}${_RST}"
        else
            continue
        fi
        [ -n "$_fresh_parts" ] && _fresh_parts="${_fresh_parts}${isep}"
        _fresh_parts="${_fresh_parts}${_label}"
    done <<EOF
$_fresh_rows
EOF
    [ -n "$_fresh_parts" ] && _freshness_segment="${_fresh_parts}"
fi

g_updates="$_freshness_segment"
# ``header``-placed contributed segments sit next to the repo-freshness segments.
if [ -n "$_seg_header" ]; then
    [ -n "$g_updates" ] && g_updates="${g_updates}${isep}"
    g_updates="${g_updates}${_seg_header}"
fi

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
            # leads no team and so renders no roster. One jq answers both
            # questions: it prints nothing at all unless this session is the
            # lead, and otherwise one ``<color>\t<name>`` line per ACTIVE
            # non-lead member (the lead excluded by agentId, which equals
            # leadAgentId, so it never lists itself even if a future config
            # stamps it isActive).
            _mates_raw=$(jq -r --arg sid "$session_id" '
                select((.leadSessionId // "") == $sid)
                | (.leadAgentId // "") as $lead
                | [ .members[]?
                    | select(((.isActive == true)) and ((.agentId // "") != $lead))
                    | "\(.color // "")\t\(.name // "")" ]
                | .[]' "$_team_cfg" 2>/dev/null)
            [ -n "$_mates_raw" ] && break
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
# Contributed segments with an unknown/dangling placement land end-of-line as
# their own trailing group (souliane/teatree#3237), never dropped or errored.
g_segend="$_seg_end"

# Join all groups with the between-group separator. There is no loop group
# (#130) — loop/tick info has exactly one home, the dedicated loop line in
# the zones file cat'd below. The mates roster (g_team) rides the header as its
# own group, after resource, so it never crowds out model/ctx/usage.
_join_groups "$g_context" "$g_usage" "$g_updates" "$g_resource" "$g_team" "$g_segend"
header="$_joined"

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

# The cheap line goes first, ALWAYS. The staleness banner used to lead every
# other line, so the moment the loop stopped writing `rendered_at` — which is
# exactly what a deliberately-parked fleet does — a red warning took line 1 and
# pushed model/RAM/CPU/disk down out of the owner's eyeline. The warning is
# honest and it stays, but it belongs directly above the frozen loop line it
# qualifies, not above the live values it says nothing about.
[ -n "$header" ] && printf '%s\n' "$header"
[ "$_skills_on_own_line" = "1" ] && printf '%s\n' "$_skills_segment"
[ -n "$_stale_banner" ] && printf '%s\n' "$_stale_banner"

# The zones file holds the dedicated loop line (and the per-overlay anchors).
# The per-session t3-master badge is PREPENDED to that loop line so the user
# reads ownership first and all loop state shares one visual home. If the zones
# file has no loop line (loops not currently live), the badge is still surfaced
# on its own trailing line so per-session ownership context is never lost.
_zones_body=""
if [ -r "$target" ]; then
    # `$(cat …)` for a file read two builtins do — and command substitution
    # strips trailing newlines, which this join reproduces.
    _zl=""
    while IFS= read -r _zl || [ -n "$_zl" ]; do
        _zones_body="${_zones_body}${_zones_body:+$'\n'}${_zl}"
    done < "$target"
fi
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
    if ! printf '%s\n' "$_zones_body" | LC_ALL=C awk -v badge="${_loop_owner_badge}${isep}" '
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
# Claude stdin JSON piped in. These are other people's scripts on the critical
# path — an unbounded one used to hang the whole bar; now it can only cost the
# tail, and the cheap line still renders.
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

return 0
}

# ─── Bounded dispatch ──────────────────────────────────────────────────────
# Runs "$@" with its stdout on $2, and kills it after $1 seconds. Returns 0
# only when the command finished on its own, so the caller can tell a complete
# result from a truncated one without trusting the output file's shape.
#
# The watchdog is killed the moment the worker is reaped, which takes its
# pending `kill` with it: a watchdog left running past this call would fire its
# TERM at a pid the kernel may since have recycled onto something else.
_run_bounded() {
    local budget="$1" outfile="$2"
    shift 2
    # Job control gives the worker its own process group, so the watchdog can
    # signal the GROUP. Signalling just the worker leaves its children running:
    # a hung chain script would then leak one orphan per statusline render, and
    # Claude Code renders the bar constantly. The plain-pid kill is the fallback
    # for a shell that refuses job control.
    set -m 2>/dev/null
    "$@" >"$outfile" 2>/dev/null &
    local worker=$!
    set +m 2>/dev/null
    ( sleep "$budget"; kill -TERM -"$worker" 2>/dev/null || kill -TERM "$worker" 2>/dev/null ) >/dev/null 2>&1 &
    local watchdog=$!
    local rc=0
    wait "$worker" 2>/dev/null || rc=$?
    kill "$watchdog" >/dev/null 2>&1
    wait "$watchdog" 2>/dev/null
    return "$rc"
}

# How long the enriched tail gets before the cheap line renders without it, and
# how long one repo's `git rev-list` gets before its cached `behind` stands.
# Both are settable so a test can make the slow path slow on purpose.
#
# 0.7s for the tail: measured on a 10-core box at load 88 the tail costs 0.1-0.6s,
# so this is not a deadline normal operation trips over — it is the one a genuinely
# stuck source trips over. The pathological total is the cheap line plus this
# budget, which still lands inside Claude Code's ~1s allowance, against a hang
# that used to be unbounded. Raising it trades a rarer dropped tail for a slower
# worst case; lowering it does the reverse.
# 0.3s for git, not less: on a loaded box a healthy `rev-list` spends most of
# that on the fork and exec alone, and a budget tight enough to kill it makes the
# hook show a cached `behind` for no reason. Degrading to the tick's cached value
# is honest either way — it is the number the field held before the recompute.
_tail_budget="${TEATREE_STATUSLINE_BUDGET:-0.7}"
_git_budget="${TEATREE_STATUSLINE_GIT_BUDGET:-0.3}"
_tmp_prefix="${TMPDIR:-/tmp}/.t3-statusline.$$"

# `SECONDS` is a builtin counter, so this backstop is free. It is deliberately set
# where only a pathology trips it: at one second it fired on a merely BUSY box and
# skipped the tail outright, which is the failure this whole change exists to
# prevent — a loaded machine permanently degraded to the cheap line. Three seconds
# spent on six local reads is a box that will not render anything useful anyway.
_tail_rendered=0
if [ "$_tail_budget" != "0" ] && [ "$SECONDS" -lt 3 ]; then
    if _run_bounded "$_tail_budget" "${_tmp_prefix}.tail" _render_tail; then
        _tail_rendered=1
    fi
fi

{
if [ "$_tail_rendered" = "1" ]; then
    _ol=""
    while IFS= read -r _ol || [ -n "$_ol" ]; do
        printf '%s\n' "$_ol"
    done < "${_tmp_prefix}.tail"
elif [ -n "$_cheap_header" ]; then
    # The tail overran or died. The line the owner actually watches is already
    # in hand, so it renders — with a marker, because silently dropping the
    # loop line, freshness and mates would read as "nothing is happening"
    # rather than "this was not read in time".
    printf '%s\n' "${_cheap_header}${gsep}${_YLW}⏳ details dropped (>${_tail_budget}s)${_RST}"
else
    # No model, no RAM, no disk and no tail: emit something rather than the
    # zero bytes Claude Code discards into a blank bar (#3233).
    printf '%s\n' "${_DIM}teatree: statusline had nothing to read${_RST}"
fi
} | _cap_line_widths

rm -f "${_tmp_prefix}.tail" "${_tmp_prefix}.git" 2>/dev/null
exit 0
