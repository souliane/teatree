#!/usr/bin/env bash

# UserPromptSubmit hook: context-aware skill suggestion with intent detection.
#
# Behavior:
# 1. Ask the Python skill loader to detect intent and project context.
#    Intent is determined by matching the prompt against triggers: patterns
#    defined in each skill's SKILL.md frontmatter (no hardcoded patterns).
# 2. Suggest the matching skill(s) if not already loaded.
# 3. In project context, also list any overlay reference injections.

input=$(cat)
session_id=$(echo "$input" | jq -r '.session_id // empty')
[ -z "$session_id" ] && exit 0

prompt=$(echo "$input" | jq -r '.prompt // empty')
[ -z "$prompt" ] && exit 0

STATE_DIR="/tmp/claude-statusline"
mkdir -p "$STATE_DIR"
skills_file="$STATE_DIR/${session_id}.skills"
active_file="$STATE_DIR/${session_id}.active"
pending_file="$STATE_DIR/${session_id}.pending"

# Clear pending suggestions from previous prompt (overwritten below if new
# suggestions are found; cleared here for the no-suggestion case).
> "$pending_file"

# --- Plugin health check (once per session) ---
symcheck_file="$STATE_DIR/${session_id}.symcheck"
if [ ! -f "$symcheck_file" ]; then
    touch "$symcheck_file"
    # Check that T3_REPO is a git repo (needed for retro/contribute)
    t3_repo="${T3_REPO:-}"
    if [ -n "$t3_repo" ] && [ -d "$t3_repo" ]; then
        if ! git -C "$t3_repo" rev-parse --git-dir >/dev/null 2>&1; then
            echo "WARNING: \$T3_REPO ($t3_repo) is NOT a git repository. Skill improvements (retro, review) will be LOST."
        fi
    fi
fi

# --- Python fast path (testable, maintainable) ---
# Try delegating intent detection + suggestion building to Python.
# Falls back to the bash logic below if Python is unavailable.

# Ensure T3_REPO is available (hooks don't source .zshrc/.teatree)
if [ -z "$T3_REPO" ] && [ -f "$HOME/.teatree" ]; then
    source "$HOME/.teatree"
fi

_SCRIPT_DIR_EARLY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
_T3_ROOT="$(cd "$_SCRIPT_DIR_EARLY/../.." 2>/dev/null && pwd -P)"
_T3_SCRIPTS="${T3_REPO:-$_T3_ROOT}/scripts"
# Parent of teatree — may contain sibling skill repos for search
_SOURCE_ROOT_EARLY="$(cd "$_T3_ROOT/.." 2>/dev/null && pwd -P)"

if [ -f "$_T3_SCRIPTS/lib/skill_loader.py" ]; then
    # Auto-populate skill metadata cache if empty or missing
    _cache_file="${XDG_DATA_HOME:-$HOME/.local/share}/teatree/skill-metadata.json"
    if [ ! -s "$_cache_file" ] || [ "$(cat "$_cache_file" 2>/dev/null)" = "{}" ]; then
        PYTHONPATH="$_T3_SCRIPTS" python3 -c "
import json
from pathlib import Path
from lib.skill_loader import build_trigger_index

skills_dir = Path.home() / '.claude' / 'skills'
index = build_trigger_index([skills_dir])
cache = Path('$_cache_file')
cache.parent.mkdir(parents=True, exist_ok=True)
cache.write_text(json.dumps({'trigger_index': index}, indent=2) + '\n', encoding='utf-8')
" 2>/dev/null
    fi

    # Build active repos list from tracker file
    _active_repos=""
    [ -f "$active_file" ] && _active_repos=$(cat "$active_file" | tr '\n' ',' | sed 's/,$//')

    # Build loaded skills list
    _loaded_skills=""
    [ -f "$skills_file" ] && _loaded_skills=$(cat "$skills_file" | tr '\n' ',' | sed 's/,$//')

    _py_result=$(PYTHONPATH="$_T3_SCRIPTS" python3 -c "
import json, sys
from lib.skill_loader import suggest_skills
data = json.loads(sys.stdin.read())
result = suggest_skills(data)
if result.get('suggestions'):
    print(json.dumps(result))
" <<EOF 2>/dev/null
{
    "prompt": $(echo "$prompt" | jq -Rs .),
    "cwd": "$PWD",
    "active_repos": [$(echo "$_active_repos" | sed 's/[^,]*/\"&\"/g')],
    "loaded_skills": [$(echo "$_loaded_skills" | sed 's/[^,]*/\"&\"/g')],
    "skill_search_dirs": [$(printf '%s\n' "$_SOURCE_ROOT_EARLY" "$HOME/.agents/skills" "$HOME/.claude/skills" | sed '/^$/d' | sed 's/.*/"&"/' | paste -sd, -)],
    "supplementary_config": "${T3_SUPPLEMENTARY_SKILLS:-$HOME/.teatree-skills.yml}"
}
EOF
    )

    if [ $? -eq 0 ] && [ -n "$_py_result" ]; then
        # Python succeeded — extract suggestions and build message
        suggest_json="$_py_result"
        py_suggest=$(echo "$suggest_json" | jq -r '.suggestions[]' 2>/dev/null)
        py_intent=$(echo "$suggest_json" | jq -r '.intent // empty' 2>/dev/null)

        if [ -n "$py_suggest" ]; then
            # Build skill list message
            skill_list=""
            while IFS= read -r s; do
                [ -z "$s" ] && continue
                if [ -n "$skill_list" ]; then
                    skill_list="${skill_list}, /${s}"
                else
                    skill_list="/${s}"
                fi
            done <<< "$py_suggest"

            msg="LOAD THESE SKILLS NOW (call the Skill tool for each, before doing anything else): ${skill_list}."

            # Persist pending suggestions for PreToolUse enforcement hook
            echo "$py_suggest" > "$pending_file"

            # Session FSM (keep in bash — lightweight)
            _SESSION_DIR="${T3_SESSION_DIR:-/tmp/t3-sessions}"
            mkdir -p "$_SESSION_DIR"
            _ticket_dir=""
            _search_dir="$PWD"
            while [ "$_search_dir" != "/" ]; do
                [ -f "$_search_dir/.env.worktree" ] && { _ticket_dir="$_search_dir"; break; }
                _search_dir=$(dirname "$_search_dir")
            done
            if [ -n "$_ticket_dir" ]; then
                _session_hash=$(echo -n "$_ticket_dir" | shasum -a 256 | cut -c1-12)
                _session_file="$_SESSION_DIR/${_session_hash}.session.json"
            else
                _session_file="$_SESSION_DIR/global.session.json"
            fi

            _skill_to_phase() {
                case "$1" in
                    ticket)   echo "scoping" ;;
                    code)     echo "coding" ;;
                    test)     echo "testing" ;;
                    debug)    echo "debugging" ;;
                    review)   echo "reviewing" ;;
                    ship)     echo "shipping" ;;
                    review-request) echo "requesting_review" ;;
                    retro)    echo "retrospecting" ;;
                    *)           echo "" ;;
                esac
            }

            target_phase=$(_skill_to_phase "$py_intent")
            if [ -n "$target_phase" ]; then
                current_phase="idle"
                visited="idle"
                if [ -f "$_session_file" ]; then
                    current_phase=$(python3 -c "import json; d=json.load(open('$_session_file')); print(d.get('state','idle'))" 2>/dev/null || echo "idle")
                    visited=$(python3 -c "import json; d=json.load(open('$_session_file')); print(' '.join(d.get('visited',[])))" 2>/dev/null || echo "idle")
                fi

                gate_warning=""
                case "$target_phase" in
                    reviewing)
                        echo "$visited" | grep -q "testing" || gate_warning="⚠ Reviewing without testing first. Run /t3:test first, or use --force in t3 ship."
                        ;;
                    shipping)
                        echo "$visited" | grep -q "testing" || gate_warning="⚠ Shipping without testing. Run /t3:test first."
                        echo "$visited" | grep -q "reviewing" || gate_warning="⚠ Shipping without reviewing. Run /t3:review first."
                        ;;
                    requesting_review)
                        echo "$visited" | grep -q "shipping" || gate_warning="⚠ Requesting review without shipping. Run /t3:ship first."
                        ;;
                esac

                python3 -c "
import json
from pathlib import Path
sf = Path('$_session_file')
data = json.loads(sf.read_text()) if sf.is_file() else {'state': 'idle', 'visited': ['idle']}
data['state'] = '$target_phase'
if '$target_phase' not in data['visited']:
    data['visited'].append('$target_phase')
sf.write_text(json.dumps(data, indent=2) + '\n')
" 2>/dev/null

                if [ -n "$gate_warning" ]; then
                    msg="$msg\n$gate_warning"
                fi
            fi

            echo "$msg"
            exit 0
        fi
    fi
fi

# Python fast path did not produce output — stay silent.
exit 0
