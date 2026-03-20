#!/usr/bin/env bash

# UserPromptSubmit hook: context-aware skill suggestion with intent detection.
#
# Behavior:
# 1. Ask the Python skill loader to detect intent and project context.
# 2. Suggest the matching skill(s) if not already loaded.
# 3. In project context, also list any overlay reference injections.
#
# The bash fallback remains for generic lifecycle intent only. Overlay
# discovery and companion-skill resolution are handled by the Python loader.

input=$(cat)
session_id=$(echo "$input" | jq -r '.session_id // empty')
[ -z "$session_id" ] && exit 0

prompt=$(echo "$input" | jq -r '.prompt // empty')
[ -z "$prompt" ] && exit 0

STATE_DIR="/tmp/claude-statusline"
mkdir -p "$STATE_DIR"
skills_file="$STATE_DIR/${session_id}.skills"
active_file="$STATE_DIR/${session_id}.active"

# --- Symlink health check (once per session) ---
symcheck_file="$STATE_DIR/${session_id}.symcheck"
if [ ! -f "$symcheck_file" ]; then
    touch "$symcheck_file"
    # Check teatree skills are symlinked (not copies)
    t3_repo="${T3_REPO:-}"
    if [ -n "$t3_repo" ]; then
        for skill_dir in "$t3_repo"/skills/t3-*/; do
            [ -d "$skill_dir" ] || continue
            skill_name=$(basename "$skill_dir")
            link="$HOME/.claude/skills/$skill_name"
            if [ -d "$link" ] && [ ! -L "$link" ]; then
                echo "WARNING: $skill_name is a copy, not a symlink. Run /t3-setup to fix."
                break
            fi
        done
    fi

    # Check that T3_REPO itself is a git repo (not a downloaded zip or stale copy)
    if [ -n "$t3_repo" ] && [ -d "$t3_repo" ]; then
        if ! git -C "$t3_repo" rev-parse --git-dir >/dev/null 2>&1; then
            echo "WARNING: \$T3_REPO ($t3_repo) is NOT a git repository. Skill improvements (retro, review) will be LOST. Clone the repo properly or run /t3-setup."
        fi
    fi

    # Check that skill symlinks point into git repos (catches non-git copies)
    for skill_link in "$HOME/.claude/skills"/t3-*; do
        [ -L "$skill_link" ] || continue
        resolved="$(readlink "$skill_link")"
        [ -d "$resolved" ] || continue
        if ! git -C "$resolved" rev-parse --git-dir >/dev/null 2>&1; then
            echo "WARNING: Skill $(basename "$skill_link") points to $resolved which is NOT a git repo. Changes will be lost. Run /t3-setup."
            break  # one warning is enough
        fi
    done

    # Check overlay project skills are installed as symlinks
    if [ -f "$HOME/.teatree.toml" ]; then
        python3 -c "
import tomllib, sys
from pathlib import Path
with open(Path.home() / '.teatree.toml', 'rb') as f:
    cfg = tomllib.load(f)
for name, overlay in cfg.get('overlays', {}).items():
    path = Path(overlay.get('path', '')).expanduser()
    if not path.is_dir():
        continue
    link_name = name if name.startswith('t3-') else f't3-{name}'
    link = Path.home() / '.claude' / 'skills' / link_name
    has_skill = any(
        (d / 'SKILL.md').is_file()
        for d in path.iterdir()
        if d.is_dir()
    )
    if has_skill and not link.exists():
        print(f'WARNING: Overlay {name} has a skill but no symlink at ~/.claude/skills/{link_name}. Run: uv run t3 doctor repair')
        sys.exit(0)  # one warning is enough
" 2>/dev/null
    fi
fi

# --- Python fast path (testable, maintainable) ---
# Try delegating intent detection + suggestion building to Python.
# Falls back to the bash logic below if Python is unavailable.
_SCRIPT_DIR_EARLY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
_SOURCE_ROOT_EARLY="$(cd "$_SCRIPT_DIR_EARLY/../../.." 2>/dev/null && pwd -P)"
_T3_SCRIPTS="${T3_REPO:-$_SOURCE_ROOT_EARLY}/scripts"

if [ -f "$_T3_SCRIPTS/lib/skill_loader.py" ]; then
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

            # Reference injections (still bash — overlay-specific YAML)
            _py_overlay_dir=$(echo "$suggest_json" | jq -r '.overlay_skill_dir // empty' 2>/dev/null)
            _py_project=$(echo "$suggest_json" | jq -r '.project_overlay // empty' 2>/dev/null)
            if [ -n "$_py_overlay_dir" ] && [ -n "$py_intent" ]; then
                injections_file="$_py_overlay_dir/hook-config/reference-injections.yml"
                if [ -f "$injections_file" ]; then
                    refs=""
                    in_skill=false
                    in_always=false
                    while IFS= read -r line; do
                        [[ "$line" =~ ^[[:space:]]*# ]] && continue
                        [[ "$line" =~ ^[[:space:]]*$ ]] && continue
                        if [[ "$line" =~ ^[a-z] ]]; then
                            skill_key="${line%%:*}"
                            if [ "$skill_key" = "$py_intent" ]; then in_skill=true; else in_skill=false; fi
                            in_always=false; continue
                        fi
                        $in_skill || continue
                        if [[ "$line" =~ ^[[:space:]]+(always|on-demand): ]]; then
                            [ "${BASH_REMATCH[1]}" = "always" ] && in_always=true || in_always=false; continue
                        fi
                        if $in_always && [[ "$line" =~ ^[[:space:]]+-[[:space:]]+(.*) ]]; then
                            ref="${BASH_REMATCH[1]}"; ref="${ref#\"}"; ref="${ref%\"}"
                            [ -n "$refs" ] && refs="${refs}, ${ref}" || refs="${ref}"
                        fi
                    done < "$injections_file"
                    if [ -n "$refs" ]; then
                        overlay_label=$(echo "$_py_project" | sed 's/^ac-//' | tr '[:lower:]' '[:upper:]')
                        msg="${msg} ${overlay_label} references to read: ${refs}"
                    fi
                fi
            fi

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
                    t3-ticket)   echo "scoping" ;;
                    t3-code)     echo "coding" ;;
                    t3-test)     echo "testing" ;;
                    t3-debug)    echo "debugging" ;;
                    t3-review)   echo "reviewing" ;;
                    t3-ship)     echo "shipping" ;;
                    t3-review-request) echo "requesting_review" ;;
                    t3-retro)    echo "retrospecting" ;;
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
                        echo "$visited" | grep -q "testing" || gate_warning="⚠ Reviewing without testing first. Run /t3-test first, or use --force in t3 ship."
                        ;;
                    shipping)
                        echo "$visited" | grep -q "testing" || gate_warning="⚠ Shipping without testing. Run /t3-test first."
                        echo "$visited" | grep -q "reviewing" || gate_warning="⚠ Shipping without reviewing. Run /t3-review first."
                        ;;
                    requesting_review)
                        echo "$visited" | grep -q "shipping" || gate_warning="⚠ Requesting review without shipping. Run /t3-ship first."
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
