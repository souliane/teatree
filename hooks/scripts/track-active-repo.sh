#!/usr/bin/env bash

# PostToolUse hook: tracks which repos the agent has touched during this session.
# Stores composite keys "branch/repo" for worktrees, plain "repo" for main repos.
# Also invalidates MR cache on git push / glab mr commands.

input=$(cat)

session_id=$(echo "$input" | jq -r '.session_id // empty')
[ -z "$session_id" ] && exit 0

tool_name=$(echo "$input" | jq -r '.tool_name // empty')

state_dir="/tmp/claude-statusline"
mkdir -p "$state_dir"

# --- MR cache invalidation on push/MR commands ---
if [[ "$tool_name" == "Bash" ]]; then
    command=$(echo "$input" | jq -r '.tool_input.command // empty')
    if [[ "$command" == *"git push"* ]] || [[ "$command" == *"glab mr"* ]]; then
        rm -f "$state_dir/${session_id}.mr_refreshed" 2>/dev/null
    fi
fi

# --- Active repo tracking ---
# Extract file path based on tool type
case "$tool_name" in
    Read|Edit|Write)
        file_path=$(echo "$input" | jq -r '.tool_input.file_path // empty')
        ;;
    Grep|Glob)
        file_path=$(echo "$input" | jq -r '.tool_input.path // empty')
        ;;
    Bash)
        # Best-effort: extract first absolute path from the command (macOS /Users or Linux /home)
        file_path=$(echo "$input" | jq -r '.tool_input.command // empty' | grep -oE '/(Users|home)/[^ "]+' | head -1)
        ;;
    *)
        exit 0
        ;;
esac

[ -z "$file_path" ] && exit 0

# Resolve workspace-relative repo name (matches statusline display keys)
workspace="${T3_WORKSPACE_DIR:-$HOME/workspace}"
if [[ "$file_path" == "$workspace/"* ]]; then
    relative="${file_path#$workspace/}"
    first="${relative%%/*}"

    repo_key="$first"
    main_repo_dir="$workspace/$first"

    # Worktree path: <ticket-dir>/<repo>/... where <ticket-dir> is not a main repo.
    if [[ ! -d "$main_repo_dir/.git" ]]; then
        rest="${relative#*/}"
        if [[ "$rest" == "$relative" ]]; then
            exit 0
        fi
        repo_in_worktree="${rest%%/*}"
        wt_dir="$workspace/$first/$repo_in_worktree"
        if [[ ! -e "$wt_dir/.git" ]]; then
            exit 0
        fi
        # Composite key: branch/repo (matches statusline wt_dir_of keys)
        wt_branch=$(git -C "$wt_dir" --no-optional-locks rev-parse --abbrev-ref HEAD 2>/dev/null)
        [ -z "$wt_branch" ] && exit 0
        repo_key="${wt_branch}/${repo_in_worktree}"
    fi

    # Append if not already tracked (dedup)
    if ! grep -qxF "$repo_key" "$state_dir/${session_id}.active" 2>/dev/null; then
        echo "$repo_key" >> "$state_dir/${session_id}.active"
    fi
fi

exit 0
