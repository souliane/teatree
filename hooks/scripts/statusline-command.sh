#!/usr/bin/env bash

# Claude Code statusline: session-focused workspace state with ticket context and GitLab links.
# Only shows worktrees relevant to the current session; hides the rest as a count.

# Require Bash 4+ (associative arrays). macOS ships 3.x; re-exec with a modern bash if available.
if (( BASH_VERSINFO[0] < 4 )); then
    for _b in /opt/homebrew/bin/bash /usr/local/bin/bash /home/linuxbrew/.linuxbrew/bin/bash; do
        [[ -x "$_b" ]] && exec "$_b" "$0" "$@"
    done
    echo "statusline: Bash 4+ required (found ${BASH_VERSION}). Install via package manager (e.g. brew install bash)." >&2
    exit 1
fi

input=$(cat)

cwd=$(echo "$input" | jq -r '.workspace.current_dir')
dir_name=$(basename "$cwd")
model=$(echo "$input" | jq -r '.model.display_name // empty')
session_id=$(echo "$input" | jq -r '.session_id // empty')
ctx_pct=$(echo "$input" | jq -r '.context_window.used_percentage // 0' | cut -d. -f1)
five_hour_pct=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // 0' | cut -d. -f1)
five_hour_resets_at=$(echo "$input" | jq -r '.rate_limits.five_hour.resets_at // empty')

WORKSPACE_ROOT="${T3_WORKSPACE_DIR:-$HOME/workspace}"

# --- Colors ---
CYAN='\033[1;36m'
YELLOW='\033[1;33m'
MAGENTA='\033[1;35m'
GREEN='\033[1;32m'
RED='\033[1;31m'
WHITE='\033[1;37m'
BLUE='\033[1;34m'
DIM='\033[0;37m'   # was \033[2m (dim/dark gray) — now plain light gray for readability
RESET='\033[0m'

# --- State directories ---
STATE_DIR="${TEATREE_CLAUDE_STATUSLINE_STATE_DIR:-/tmp/claude-statusline}"
MR_CACHE_DIR="$STATE_DIR/mr_cache"
mkdir -p "$STATE_DIR" "$MR_CACHE_DIR"
find "$STATE_DIR" -maxdepth 1 -type f -mmin +1440 -delete 2>/dev/null
find "$MR_CACHE_DIR" -maxdepth 1 -type f -mmin +1440 -delete 2>/dev/null

persist_telemetry() {
    local now_utc latest_file
    now_utc=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    latest_file="$STATE_DIR/latest-telemetry.json"
    jq -n \
        --arg session_id "$session_id" \
        --arg cwd "$cwd" \
        --arg model "$model" \
        --arg five_hour_resets_at "$five_hour_resets_at" \
        --arg updated_at "$now_utc" \
        --argjson context_window_used_percentage "$ctx_pct" \
        --argjson five_hour_used_percentage "$five_hour_pct" \
        '{
            session_id: $session_id,
            cwd: $cwd,
            model: $model,
            context_window_used_percentage: $context_window_used_percentage,
            five_hour_used_percentage: $five_hour_used_percentage,
            five_hour_resets_at: $five_hour_resets_at,
            updated_at: $updated_at
        }' >| "$latest_file"
    if [ -n "$session_id" ]; then
        cp "$latest_file" "$STATE_DIR/${session_id}.telemetry.json"
    fi
}

persist_telemetry

get_dirty_hash() {
    local output
    output=$(git -C "$1" --no-optional-locks status --porcelain 2>/dev/null)
    if command -v md5sum >/dev/null 2>&1; then
        echo "$output" | md5sum | cut -d' ' -f1
    else
        echo "$output" | md5 -q
    fi
}

# Sanitize branch name for use in filenames (replace / with __)
safe_name() { echo "${1//\//__}"; }

# --- Collect repos + worktrees ---
declare -A repo_dir repo_branch repo_on_default repo_gitlab_url
declare -A branch_repos wt_dir_of

for d in "$WORKSPACE_ROOT"/*/; do
    [ -d "$d/.git" ] || continue
    repo_name=$(basename "$d")
    repo_dir[$repo_name]="$d"

    branch=$(git -C "$d" --no-optional-locks rev-parse --abbrev-ref HEAD 2>/dev/null) || continue
    repo_branch[$repo_name]="$branch"

    # GitLab URL from remote
    remote_url=$(git -C "$d" --no-optional-locks remote get-url origin 2>/dev/null)
    if [[ "$remote_url" =~ git@([^:]+):(.+)\.git$ ]]; then
        repo_gitlab_url[$repo_name]="https://${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
    fi

    # Default branch detection
    default_branch=$(git -C "$d" --no-optional-locks symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||')
    if [ -n "$default_branch" ]; then
        [[ "$branch" == "$default_branch" ]] && repo_on_default[$repo_name]=1
    else
        [[ "$branch" == "main" || "$branch" == "master" || "$branch" == "development" ]] && repo_on_default[$repo_name]=1
    fi

    # Worktrees (skip main worktree = first line)
    first=true
    while IFS= read -r line; do
        if $first; then first=false; continue; fi
        wt_path=$(echo "$line" | awk '{print $1}')
        wt_branch=$(echo "$line" | grep -o '\[.*\]' | tr -d '[]')
        [ -d "$wt_path" ] || continue
        [ -n "$wt_branch" ] || continue

        wt_dir_of["${wt_branch}/${repo_name}"]="$wt_path/"

        if [ -n "${branch_repos[$wt_branch]}" ]; then
            branch_repos[$wt_branch]="${branch_repos[$wt_branch]} ${repo_name}"
        else
            branch_repos[$wt_branch]="$repo_name"
        fi
    done < <(git -C "$d" --no-optional-locks worktree list 2>/dev/null)
done

# --- Look up TICKET_URL from .env.worktree for a given branch ---
get_ticket_url_from_env() {
    local branch="$1"
    for key in "${!wt_dir_of[@]}"; do
        if [[ "$key" == "${branch}/"* ]]; then
            local wt_path="${wt_dir_of[$key]}"
            local env_file="$(dirname "$wt_path")/.env.worktree"
            if [ -f "$env_file" ]; then
                local url
                url=$(grep -m1 '^TICKET_URL=' "$env_file" | cut -d= -f2-)
                [ -n "$url" ] && echo "$url" && return 0
            fi
        fi
    done
    return 1
}

# --- Extract ticket info from branch name ---
# Convention: <prefix>-<repo>-<ticket_number>-<description>
# Returns: repo|number|description  (repo may be empty for fallback matches)
extract_ticket_info() {
    local branch="$1"
    # Sort known repos by name length (longest first) to avoid partial matches
    local sorted_repos
    sorted_repos=$(for r in "${!repo_dir[@]}"; do echo "$r"; done | awk '{print length, $0}' | sort -rn | awk '{print $2}')

    for rn in $sorted_repos; do
        if [[ "$branch" =~ -${rn}-([0-9]+)-(.*) ]]; then
            echo "${rn}|${BASH_REMATCH[1]}|${BASH_REMATCH[2]}"
            return 0
        fi
        if [[ "$branch" =~ -${rn}-([0-9]+)$ ]]; then
            echo "${rn}|${BASH_REMATCH[1]}|"
            return 0
        fi
    done

    # Fallback: any 3+ digit number in the branch name
    if [[ "$branch" =~ -([0-9]{3,})-(.*) ]]; then
        echo "|${BASH_REMATCH[1]}|${BASH_REMATCH[2]}"
        return 0
    fi
    if [[ "$branch" =~ -([0-9]{3,})$ ]]; then
        echo "|${BASH_REMATCH[1]}|"
        return 0
    fi

    return 1
}

# --- Format ticket header for a branch ---
# Output: colored string with optional OSC 8 clickable link for ticket number
format_ticket_header() {
    local branch="$1"
    local ticket_info
    ticket_info=$(extract_ticket_info "$branch")

    if [ -n "$ticket_info" ]; then
        local t_repo t_num t_desc
        IFS='|' read -r t_repo t_num t_desc <<< "$ticket_info"

        local header=""
        if [ -n "$t_num" ] && [ "$t_num" != "0000" ]; then
            # Prefer TICKET_URL from .env.worktree (authoritative) over branch-name heuristic
            local issue_url=""
            local env_url
            env_url=$(get_ticket_url_from_env "$branch")
            if [ -n "$env_url" ]; then
                issue_url="$env_url"
            elif [ -n "$t_repo" ] && [ -n "${repo_gitlab_url[$t_repo]}" ]; then
                issue_url="${repo_gitlab_url[$t_repo]}/-/issues/${t_num}"
            fi

            if [ -n "$issue_url" ]; then
                header="\033]8;;${issue_url}\033\\\\${WHITE}#${t_num}${RESET}\033]8;;\033\\\\"
            else
                header="${WHITE}#${t_num}${RESET}"
            fi
        fi

        [ -n "$t_desc" ] && header="${header} ${DIM}${t_desc}${RESET}"
        echo "$header"
    else
        echo "${MAGENTA}${branch}${RESET}"
    fi
}

# --- Session snapshot (first render only) ---
if [ -n "$session_id" ] && [ ! -f "$STATE_DIR/$session_id" ]; then
    {
        for rn in "${!repo_dir[@]}"; do
            echo "${rn}:$(get_dirty_hash "${repo_dir[$rn]}")"
        done
        for key in "${!wt_dir_of[@]}"; do
            echo "${key}:$(get_dirty_hash "${wt_dir_of[$key]}")"
        done
    } > "$STATE_DIR/$session_id"
fi

declare -A initial_hash
if [ -n "$session_id" ] && [ -f "$STATE_DIR/$session_id" ]; then
    while IFS=: read -r name hash; do
        initial_hash[$name]="$hash"
    done < "$STATE_DIR/$session_id"
fi

# --- Read active repos/branches from hook ---
declare -A active_repos active_branches
if [ -n "$session_id" ] && [ -f "$STATE_DIR/${session_id}.active" ]; then
    while IFS= read -r _r; do
        [ -n "$_r" ] || continue
        active_repos[$_r]=1
        # Extract branch from composite key "branch/repo"
        if [[ "$_r" == *"/"* ]]; then
            active_branches["${_r%%/*}"]=1
        fi
    done < "$STATE_DIR/${session_id}.active"
fi

# --- Detect cwd context (ticket directory) ---
# A ticket directory is a top-level dir under WORKSPACE_ROOT that is NOT a main
# repo (no .git/).  When we're inside one, ALL worktrees in that directory are
# relevant and should be shown — not just session-active ones.
cwd_ticket_dir=""
cwd_ticket_branch=""
if [[ "$cwd" == "$WORKSPACE_ROOT/"* ]]; then
    relative="${cwd#$WORKSPACE_ROOT/}"
    first_segment="${relative%%/*}"
    candidate="$WORKSPACE_ROOT/$first_segment"

    if [ -d "$candidate" ] && [ ! -d "$candidate/.git" ]; then
        cwd_ticket_dir="$candidate"
    fi

    # Ticket directory = known worktree branch (not a main repo)
    if [[ -n "${branch_repos[$first_segment]+x}" ]]; then
        cwd_ticket_branch="$first_segment"
    fi
fi

# --- Background MR cache refresh (once per session, re-triggered on push/MR) ---
if [ -n "$session_id" ] && [ ! -f "$STATE_DIR/${session_id}.mr_refreshed" ]; then
    touch "$STATE_DIR/${session_id}.mr_refreshed"
    (
        for wt_branch in "${!branch_repos[@]}"; do
            for rn in ${branch_repos[$wt_branch]}; do
                cache_file="$MR_CACHE_DIR/$(safe_name "$wt_branch")__${rn}"
                # Skip if cache is fresh (< 30 min)
                [ -f "$cache_file" ] && [ -n "$(find "$cache_file" -mmin -30 2>/dev/null)" ] && continue

                dir="${repo_dir[$rn]}"
                [ -z "$dir" ] && continue
                mr_url=$(cd "$dir" && glab mr list --source-branch "$wt_branch" -F json 2>/dev/null | jq -r '.[0].web_url // empty')
                if [ -n "$mr_url" ]; then
                    echo "$mr_url" > "$cache_file"
                else
                    : > "$cache_file"  # empty = no MR
                fi
            done
        done
    ) &
fi

# --- Build display ---
items=""

# 1. Main repos: show if non-default branch, session dirty, or active
for repo_name in $(for r in "${!repo_dir[@]}"; do echo "$r"; done | sort); do
    dir="${repo_dir[$repo_name]}"
    branch="${repo_branch[$repo_name]}"
    on_default=false
    [ -n "${repo_on_default[$repo_name]+x}" ] && on_default=true

    session_dirty=""
    if [ -n "$session_id" ] && [ -n "${initial_hash[$repo_name]+x}" ]; then
        current_hash=$(get_dirty_hash "$dir")
        if [ "${initial_hash[$repo_name]}" != "$current_hash" ]; then
            session_dirty="${RED}*${RESET}"
        fi
    fi

    is_active=false
    active_marker=""
    [[ -n "${active_repos[$repo_name]+x}" ]] && is_active=true && active_marker="${GREEN}>${RESET}"

    # Only show repos that are session-dirty or session-active
    [ -z "$session_dirty" ] && ! $is_active && continue

    if $on_default; then
        items="${items}\n ${CYAN}${repo_name}${RESET}${session_dirty}${active_marker}"
    else
        items="${items}\n ${YELLOW}${repo_name}${RESET}${session_dirty}${active_marker} ${DIM}on${RESET} ${YELLOW}${branch}${RESET}"
    fi
done

# 2. Worktrees: show all worktrees for relevant tickets
# A ticket is relevant if: we're in its directory, agent touched it, or it's session-dirty.
# Once a ticket is relevant, ALL its repo worktrees are shown.
declare -a visible_wt_entries  # "timestamp|branch" for sorting by last modified

for wt_branch in "${!branch_repos[@]}"; do
    is_visible=false

    # Always show worktrees physically inside the current ticket directory
    if [ -n "$cwd_ticket_dir" ]; then
        for rn in ${branch_repos[$wt_branch]}; do
            key="${wt_branch}/${rn}"
            if [[ "${wt_dir_of[$key]}" == "$cwd_ticket_dir/"* ]]; then
                is_visible=true
                break
            fi
        done
    fi

    # Check if session-active (agent touched files in this worktree)
    ! $is_visible && [[ -n "${active_branches[$wt_branch]+x}" ]] && is_visible=true

    # Check if session-dirty (files changed since session start)
    # Only compare repos that were captured in the session snapshot — entries
    # not in the snapshot predate this session and should not be shown.
    if ! $is_visible && [ -n "$session_id" ]; then
        for rn in ${branch_repos[$wt_branch]}; do
            key="${wt_branch}/${rn}"
            if [ -n "${wt_dir_of[$key]}" ] && [ -n "${initial_hash[$key]+x}" ]; then
                current_hash=$(get_dirty_hash "${wt_dir_of[$key]}")
                if [ "${initial_hash[$key]}" != "$current_hash" ]; then
                    is_visible=true
                    break
                fi
            fi
        done
    fi

    $is_visible || continue

    # Get modification time for sorting (most recent repo in the worktree)
    latest_mtime=0
    for rn in ${branch_repos[$wt_branch]}; do
        key="${wt_branch}/${rn}"
        wt_path="${wt_dir_of[$key]}"
        [ -z "$wt_path" ] && continue
        if [[ "$OSTYPE" == "darwin"* ]]; then
            mtime=$(stat -f '%m' "$wt_path" 2>/dev/null || echo 0)
        else
            mtime=$(stat -c '%Y' "$wt_path" 2>/dev/null || echo 0)
        fi
        (( mtime > latest_mtime )) && latest_mtime=$mtime
    done

    visible_wt_entries+=("${latest_mtime}|${wt_branch}")
done

# Sort by modification time (most recent first)
sorted_wt_branches=()
if [ ${#visible_wt_entries[@]} -gt 0 ]; then
    while IFS='|' read -r _ts _branch; do
        sorted_wt_branches+=("$_branch")
    done < <(printf '%s\n' "${visible_wt_entries[@]}" | sort -t'|' -k1 -rn)
fi

# Render sorted visible worktrees — group branches with the same ticket number on one line.
# Build a map from ticket_number -> list of "wt_branch" entries (preserving sort order).
declare -A ticket_num_branches   # ticket_num -> space-separated list of wt_branches
declare -a ticket_num_order      # ordered unique ticket numbers

for wt_branch in "${sorted_wt_branches[@]}"; do
    ticket_info=$(extract_ticket_info "$wt_branch")
    t_num=""
    if [ -n "$ticket_info" ]; then
        IFS='|' read -r _t_repo t_num _t_desc <<< "$ticket_info"
    fi
    # Fall back to branch name as key when no ticket number found
    group_key="${t_num:-$wt_branch}"

    if [ -z "${ticket_num_branches[$group_key]+x}" ]; then
        ticket_num_order+=("$group_key")
        ticket_num_branches[$group_key]="$wt_branch"
    else
        ticket_num_branches[$group_key]="${ticket_num_branches[$group_key]} $wt_branch"
    fi
done

for group_key in "${ticket_num_order[@]}"; do
    group_branches="${ticket_num_branches[$group_key]}"

    # Use the first branch to build the ticket header (they share ticket # and description)
    first_branch="${group_branches%% *}"
    ticket_header=$(format_ticket_header "$first_branch")

    repo_list=""
    mr_links=""
    any_current=false

    for wt_branch in $group_branches; do
        repos="${branch_repos[$wt_branch]}"
        for rn in $repos; do
            key="${wt_branch}/${rn}"

            repo_dirty=""
            if [ -n "$session_id" ] && [ -n "${wt_dir_of[$key]}" ] && [ -n "${initial_hash[$key]+x}" ]; then
                current_hash=$(get_dirty_hash "${wt_dir_of[$key]}")
                if [ "${initial_hash[$key]}" != "$current_hash" ]; then
                    repo_dirty="${RED}*${RESET}"
                fi
            fi

            repo_active=""
            [[ -n "${active_repos[$key]+x}" ]] && repo_active="${GREEN}>${RESET}"

            # Mark the repo whose worktree contains the current working directory
            is_current_repo=false
            if [ -n "${wt_dir_of[$key]}" ] && [[ "$cwd" == "${wt_dir_of[$key]%/}"* ]]; then
                is_current_repo=true
                any_current=true
            fi

            if $is_current_repo; then
                repo_list="${repo_list} ${GREEN}>>${RESET} ${CYAN}${rn}${RESET}${repo_dirty}${repo_active}"
            else
                repo_list="${repo_list} ${CYAN}${rn}${RESET}${repo_dirty}${repo_active}"
            fi

            # MR link (clickable via OSC 8)
            cache_file="$MR_CACHE_DIR/$(safe_name "$wt_branch")__${rn}"
            if [ -f "$cache_file" ] && [ -s "$cache_file" ]; then
                mr_url=$(cat "$cache_file")
                mr_links="${mr_links} \033]8;;${mr_url}\033\\\\${BLUE}MR${RESET}\033]8;;\033\\\\"
            fi
        done
    done

    items="${items}\n  ${ticket_header}${repo_list}"
    [ -n "$mr_links" ] && items="${items} ${DIM}|${RESET}${mr_links}"
done

# Count hidden worktrees
total_wt=${#branch_repos[@]}
shown_wt=${#sorted_wt_branches[@]}
hidden_wt=$((total_wt - shown_wt))
(( hidden_wt > 0 )) && items="${items}\n ${DIM}+${hidden_wt} other worktrees${RESET}"

# --- Collect session-used skills ---
skills=""
if [ -n "$session_id" ] && [ -f "$STATE_DIR/${session_id}.skills" ]; then
    while IFS= read -r skill_name; do
        [ -z "$skill_name" ] && continue
        if [ -n "$skills" ]; then
            skills="${skills} ${DIM}|${RESET} ${MAGENTA}${skill_name}${RESET}"
        else
            skills="${MAGENTA}${skill_name}${RESET}"
        fi
    done < "$STATE_DIR/${session_id}.skills"
fi

# --- Output ---
# Model
[ -n "$model" ] && printf "model=${GREEN}%s${RESET} | " "$model"

# Context window usage (color-coded: green < 50%, yellow 50-80%, red > 80%)
if (( ctx_pct > 80 )); then
    printf "ctx=${RED}%s%%${RESET} | " "$ctx_pct"
elif (( ctx_pct > 50 )); then
    printf "ctx=${YELLOW}%s%%${RESET} | " "$ctx_pct"
else
    printf "ctx=${DIM}%s%%${RESET} | " "$ctx_pct"
fi

# Five-hour Claude usage — human-readable with reset time in local timezone
rate_reset=""
if [ -n "$five_hour_resets_at" ]; then
    tz_name="${T3_TIMEZONE:-$(date +%Z)}"
    if [[ "$five_hour_resets_at" =~ ^[0-9]+$ ]]; then
        # Unix epoch (seconds)
        if [[ "$OSTYPE" == "darwin"* ]]; then
            reset_time=$(date -j -r "$five_hour_resets_at" "+%H:%M" 2>/dev/null)
        else
            reset_time=$(date -d "@$five_hour_resets_at" "+%H:%M" 2>/dev/null)
        fi
    else
        # ISO 8601
        if [[ "$OSTYPE" == "darwin"* ]]; then
            reset_time=$(date -j -f "%Y-%m-%dT%H:%M:%S" "${five_hour_resets_at%%[.Z]*}" "+%H:%M" 2>/dev/null)
        else
            reset_time=$(date -d "$five_hour_resets_at" "+%H:%M" 2>/dev/null)
        fi
    fi
    [ -n "$reset_time" ] && rate_reset=" until ${reset_time} ${tz_name}"
fi

if (( five_hour_pct >= 95 )); then
    printf "${RED}%s%%${RESET}%s | " "$five_hour_pct" "$rate_reset"
elif (( five_hour_pct >= 80 )); then
    printf "${YELLOW}%s%%${RESET}%s | " "$five_hour_pct" "$rate_reset"
else
    printf "${DIM}%s%%${RESET}%s | " "$five_hour_pct" "$rate_reset"
fi

# CWD with ticket context when inside a worktree/ticket directory
if [ -n "$cwd_ticket_branch" ]; then
    ticket_header=$(format_ticket_header "$cwd_ticket_branch")
    if [[ "$dir_name" == "$cwd_ticket_branch" ]]; then
        printf "%b" "$ticket_header"
    else
        printf "%b ${DIM}/${RESET} ${CYAN}%s${RESET}" "$ticket_header" "$dir_name"
    fi
elif [ -n "$cwd_ticket_dir" ]; then
    # In a ticket directory but branch not matched — show directory name
    ticket_dir_name=$(basename "$cwd_ticket_dir")
    if [[ "$dir_name" == "$ticket_dir_name" ]]; then
        printf "${YELLOW}%s${RESET}" "$ticket_dir_name"
    else
        printf "${YELLOW}%s${RESET} ${DIM}/${RESET} ${CYAN}%s${RESET}" "$ticket_dir_name" "$dir_name"
    fi
else
    printf "cwd=${CYAN}%s${RESET}" "$dir_name"
fi

# Skills
[ -n "$skills" ] && printf " | skills: %b" "$skills"

# Items
[ -n "$items" ] && printf " |%b" "$items"

exit 0
