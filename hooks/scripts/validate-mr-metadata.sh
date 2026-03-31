#!/usr/bin/env bash

# PreToolUse hook: validates MR title/description against project-specific rules.
# Blocks non-compliant glab mr create/update commands with a clear error.
#
# Set T3_MR_VALIDATE_SCRIPT to a Python script that accepts --title and --description.
# If not set or missing, this hook is a no-op (allows all MRs through).
#
# Stdin JSON: { "tool_name", "tool_input", "session_id" }
# Exit 0 silently = allow.  JSON with permissionDecision=deny on stdout = block.

set -euo pipefail

# --- Find validation script ---
VALIDATE_SCRIPT="${T3_MR_VALIDATE_SCRIPT:-}"

# No validator found if T3_MR_VALIDATE_SCRIPT is not set.
# Project overlays should set this env var to their validation script.

# No validator found — allow everything through.
[ -z "$VALIDATE_SCRIPT" ] || [ ! -f "$VALIDATE_SCRIPT" ] && exit 0

# --- Parse input ---
input=$(cat)
tool_name=$(echo "$input" | jq -r '.tool_name // empty')

title=""
description=""

case "$tool_name" in
    Bash)
        command=$(echo "$input" | jq -r '.tool_input.command // empty')
        [[ "$command" != *"glab mr create"* && "$command" != *"glab mr update"* ]] && exit 0

        # Extract --title
        if [[ "$command" =~ --title[[:space:]]+[\"\']([^\"\']+)[\"\'] ]]; then
            title="${BASH_REMATCH[1]}"
        elif [[ "$command" =~ --title[[:space:]]+\$\'([^\']+)\' ]]; then
            title="${BASH_REMATCH[1]}"
        fi

        # Extract --description (heredoc or quoted)
        if [[ "$command" =~ --description[[:space:]]+\"\$\(cat[[:space:]]+\<\<[\']?EOF[\']? ]]; then
            description=$(echo "$command" | sed -n '/--description.*<<.*EOF/,/^EOF/{/--description/d;/^EOF/d;p;}')
        elif [[ "$command" =~ --description[[:space:]]+[\"\']([^\"\']+)[\"\'] ]]; then
            description="${BASH_REMATCH[1]}"
        elif [[ "$command" =~ --description[[:space:]]+\$\'([^\']+)\' ]]; then
            description="${BASH_REMATCH[1]}"
        fi
        ;;

    mcp__glab__glab_mr_create|mcp__glab__glab_mr_update)
        title=$(echo "$input" | jq -r '.tool_input.title // empty')
        description=$(echo "$input" | jq -r '.tool_input.description // empty')
        ;;

    *)
        exit 0
        ;;
esac

[ -z "$title" ] && exit 0

# --- Validate ---
errors=$(python3 "$VALIDATE_SCRIPT" --title "$title" --description "$description" 2>&1) && exit 0

# --- Deny with reason ---
json_errors=$(printf '%s' "$errors" | jq -Rs .)
cat <<ENDJSON
{"permissionDecision": "deny", "permissionDecisionReason": $json_errors}
ENDJSON
