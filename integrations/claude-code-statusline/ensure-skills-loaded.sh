#!/usr/bin/env bash

# UserPromptSubmit hook: context-aware skill suggestion with intent detection.
#
# Behavior:
# 1. Discover project overlay skills via hook-config/context-match.yml convention.
# 2. Parse the user's prompt to detect which lifecycle phase they need.
# 3. Suggest the matching skill(s) if not already loaded.
# 4. In project context, also list which references should be loaded
#    (read from <overlay>/hook-config/reference-injections.yml).
#
# Outside project context: still suggests generic t3-* skills based on intent.

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
    if [ -n "$t3_repo" ] && [ -d "$HOME/.agents/skills/teatree" ] && [ ! -L "$HOME/.agents/skills/teatree" ]; then
        if [ -x "$t3_repo/scripts/install_skills.sh" ]; then
            "$t3_repo/scripts/install_skills.sh" 2>/dev/null
            echo "Teatree skill symlinks were broken (copies detected). Auto-fixed via scripts/install_skills.sh."
        else
            echo "WARNING: Teatree skills are copies, not symlinks. Run: \$T3_REPO/scripts/install_skills.sh"
        fi
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
fi

# --- Project overlay discovery via hook-config/context-match.yml ---
# Scan all skill directories for context-match.yml files. If any pattern
# matches $PWD or the active-repo tracker, that skill is a project overlay.
project_context=false
project_overlay=""

_detect_overlay() {
    local skill_dir="$1" skill_name="$2"
    local match_file="$skill_dir/hook-config/context-match.yml"
    [ -f "$match_file" ] || return 1

    # Parse cwd_patterns from YAML (simple line-based parser)
    local in_patterns=false
    while IFS= read -r line; do
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ "$line" =~ ^[[:space:]]*$ ]] && continue

        if [[ "$line" =~ ^cwd_patterns: ]]; then
            in_patterns=true; continue
        fi
        # Any other top-level key ends the patterns section
        if [[ "$line" =~ ^[a-z] ]]; then
            in_patterns=false; continue
        fi

        if $in_patterns && [[ "$line" =~ ^[[:space:]]+-[[:space:]]+(.*) ]]; then
            local pat="${BASH_REMATCH[1]}"
            pat="${pat#\"}"; pat="${pat%\"}"  # strip quotes

            # Match against PWD
            if [[ "$PWD" == *"$pat"* ]]; then
                project_overlay="$skill_name"
                return 0
            fi
            # Match against active-repo tracker
            if [ -f "$active_file" ] && grep -qF "$pat" "$active_file" 2>/dev/null; then
                project_overlay="$skill_name"
                return 0
            fi
        fi
    done < "$match_file"
    return 1
}

# Derive source repo root from this script's real path (resolves symlinks).
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
_SOURCE_ROOT="$(cd "$_SCRIPT_DIR/../../.." 2>/dev/null && pwd -P)"

# Search skill directories (source of truth first, then published, then symlinked)
for skills_root in \
    "$_SOURCE_ROOT" \
    "$HOME/.agents/skills" \
    "$HOME/.claude/skills"; do
    [ -d "$skills_root" ] || continue
    for candidate_dir in "$skills_root"/*/; do
        [ -d "$candidate_dir" ] || continue
        skill_name="$(basename "$candidate_dir")"
        if _detect_overlay "$candidate_dir" "$skill_name"; then
            project_context=true
            break 2
        fi
    done
done

# Fallback: if no overlay detected from context scanning but T3_OVERLAY is set,
# use it as the default overlay. This covers fresh sessions where $PWD is generic
# and the active-repo tracker hasn't been populated yet.
if ! $project_context && [ -n "${T3_OVERLAY:-}" ] && [ -d "$T3_OVERLAY/hook-config" ]; then
    project_overlay="$(basename "$T3_OVERLAY")"
    project_context=true
fi

# Resolve overlay skill directory (source of truth first, then published, then symlinked)
overlay_skill_dir=""
if $project_context && [ -n "$project_overlay" ]; then
    for candidate in \
        "${T3_OVERLAY:-__none__}" \
        "$_SOURCE_ROOT/$project_overlay" \
        "$HOME/.agents/skills/$project_overlay" \
        "$HOME/.claude/skills/$project_overlay"; do
        if [ -d "$candidate/hook-config" ]; then
            overlay_skill_dir="$candidate"
            break
        fi
    done
fi

# --- URL detection: issue tracker, external docs, error tracking links ---
detect_url_intent() {
    local lp="$1"

    # Issue tracker URLs → t3-ticket (ticket intake)
    # GitLab: gitlab.com/.../-(issues|merge_requests|jobs)/123
    # GitHub: github.com/.../issues/123 or github.com/.../pull/123
    if echo "$lp" | grep -qE 'https?://gitlab\.[^[:space:]]+/-(issues|merge_requests|jobs)/[0-9]+'; then
        echo "t3-ticket"; return
    fi
    if echo "$lp" | grep -qE 'https?://github\.com/[^[:space:]]+/(issues|pull)/[0-9]+'; then
        echo "t3-ticket"; return
    fi

    # External doc/wiki URLs → t3-ticket (context gathering)
    # Notion, Confluence, Linear, etc.
    if echo "$lp" | grep -qE 'https?://(www\.)?notion\.(so|site)/'; then
        echo "t3-ticket"; return
    fi
    if echo "$lp" | grep -qE 'https?://[^[:space:]]*\.atlassian\.net/wiki/'; then
        echo "t3-ticket"; return
    fi
    if echo "$lp" | grep -qE 'https?://linear\.app/[^[:space:]]+/issue/'; then
        echo "t3-ticket"; return
    fi

    # Error tracking URLs → t3-debug
    if echo "$lp" | grep -qE 'https?://[^[:space:]]*sentry\.[^[:space:]]+/issues/'; then
        echo "t3-debug"; return
    fi

    # Overlay-provided URL patterns (from hook-config/url-patterns.yml)
    if [ -n "$overlay_skill_dir" ] && [ -f "$overlay_skill_dir/hook-config/url-patterns.yml" ]; then
        local current_intent=""
        while IFS= read -r line; do
            [[ "$line" =~ ^[[:space:]]*# ]] && continue
            [[ "$line" =~ ^[[:space:]]*$ ]] && continue
            # Intent key (e.g., "t3-ticket:")
            if [[ "$line" =~ ^([a-z0-9-]+): ]]; then
                current_intent="${BASH_REMATCH[1]}"
                continue
            fi
            # Pattern list item
            if [ -n "$current_intent" ] && [[ "$line" =~ ^[[:space:]]+-[[:space:]]+(.*) ]]; then
                local pat="${BASH_REMATCH[1]}"
                pat="${pat#\"}"; pat="${pat%\"}"
                if echo "$lp" | grep -qE "$pat"; then
                    echo "$current_intent"; return
                fi
            fi
        done < "$overlay_skill_dir/hook-config/url-patterns.yml"
    fi

    echo ""
}

# --- Intent detection from prompt keywords ---
# Returns the skill name that best matches the user's intent.
# Order matters: more specific patterns are checked first.
detect_intent() {
    local p="$1"
    # Normalize to lowercase for matching
    local lp
    lp=$(echo "$p" | tr '[:upper:]' '[:lower:]')

    # URL-based detection first (most specific)
    local url_intent
    url_intent=$(detect_url_intent "$lp")
    if [ -n "$url_intent" ]; then
        echo "$url_intent"; return
    fi

    # t3-ship: delivery actions
    if echo "$lp" | grep -qE '\b(merge request|pull request|create an? (mr|pr)|\bmr\b|push\b|finalize|deliver|ship it|create mr|create pr)\b'; then
        echo "t3-ship"; return
    fi
    # t3-ship: commit (but not "review comment")
    if echo "$lp" | grep -qE '\bcommit\b' && ! echo "$lp" | grep -qE '\breview\b'; then
        echo "t3-ship"; return
    fi

    # t3-test: testing and CI
    if echo "$lp" | grep -qE '\b(run.*tests?|pytest|lint|sonar|e2e|ci fail|pipeline fail|what tests|tests? broke|test runner)\b'; then
        echo "t3-test"; return
    fi
    # t3-test: "pipeline failed/failure/is red" (allow word suffixes after "fail" and "pipeline")
    if echo "$lp" | grep -qE '\bpipeline\b.*(fail|red|broke)'; then
        echo "t3-test"; return
    fi

    # t3-review-request: request human review (check before t3-review — more specific)
    if echo "$lp" | grep -qE '\b(request review|ask for review|send.* review|notify reviewer|post mr|review request)\b'; then
        echo "t3-review-request"; return
    fi

    # t3-review: code review (self-review, giving review, receiving feedback)
    if echo "$lp" | grep -qE '\b(review|check the code|check my code|feedback|quality check|code review)\b'; then
        echo "t3-review"; return
    fi

    # t3-debug: troubleshooting
    if echo "$lp" | grep -qE "\b(broken|error|not working|crash|blank page|can.t connect|debug|fix this|won.t start|500|traceback|exception)\b"; then
        echo "t3-debug"; return
    fi

    # t3-ticket: ticket intake (check before t3-code since "implement TICKET-1234" is intake)
    if echo "$lp" | grep -qE '(new ticket|start working|what should i do)'; then
        echo "t3-ticket"; return
    fi
    # Generic ticket/issue patterns (PROJ-1234, ticket #123, issue 456)
    if echo "$lp" | grep -qE '([a-z]+-[0-9]+|\b(ticket|issue) #?[0-9]+)'; then
        echo "t3-ticket"; return
    fi

    # t3-code: implementation and code changes (broad — most prompts are about coding)
    if echo "$lp" | grep -qE '\b(implement|code it|feature|refactor|rework|restructure|rewrite|redesign)\b'; then
        echo "t3-code"; return
    fi
    # t3-code: verb + article/pronoun patterns ("fix the X", "add a Y", "change the Z")
    if echo "$lp" | grep -qE '\b(fix|change|update|modify|adjust|add|remove|delete|write|create|build|move|rename|extract|split|merge|convert|migrate|optimize|improve|replace|swap|introduce|drop|deprecate|wire|hook up|integrate|extend|override|wrap|unwrap|inline|deduplicate|dedup|simplify|generalize|normalize|transform|adapt|port|backport|scaffold|stub|mock|patch|hotfix|tweak|rework|clean) (the|a|an|this|that|my|our|its|some|all|each|every)\b'; then
        echo "t3-code"; return
    fi
    # t3-code: bare imperative verbs at start of prompt ("Fix login", "Add validation")
    if echo "$lp" | grep -qE '^(fix|change|update|modify|adjust|add|remove|delete|write|create|build|move|rename|extract|refactor|replace|introduce|extend|override|simplify|optimize|improve|implement|convert|migrate|integrate|wire|hook|patch|hotfix|tweak|rework|clean up|scaffold|stub|mock|deduplicate|dedup) '; then
        echo "t3-code"; return
    fi

    # t3-setup: first-time skills installation/configuration (check BEFORE t3-workspace to avoid "setup" collision)
    if echo "$lp" | grep -qE '\b(setup skills|configure claude|install skills|bootstrap skills|configure hooks)\b'; then
        echo "t3-setup"; return
    fi

    # t3-contribute: push improvements to fork / upstream issues
    if echo "$lp" | grep -qE '\b(t3.?contribute|push improvements?|push skills?|contribute upstream)\b'; then
        echo "t3-contribute"; return
    fi

    # t3-retro: retrospective and skill improvement
    if echo "$lp" | grep -qE '\b(retro|retrospective|lessons learned|improve skills?|auto.?improve|what went wrong)\b'; then
        echo "t3-retro"; return
    fi

    # t3-followup: daily follow-up, batch tickets, status checks, MR reminders
    if echo "$lp" | grep -qE '\b(follow.?up|autopilot|batch tickets?|process all tickets|not started issues?|work on all my tickets|check (ticket )?status|advance tickets?|remind reviewers?|mr reminders?|nudge)\b'; then
        echo "t3-followup"; return
    fi

    # t3-workspace: environment/infrastructure (allow plural "servers", "databases", "passwords")
    if echo "$lp" | grep -qE '\b(worktree|setup|servers?|start session|refresh db|cleanup|clean up|reset passwords?|t3_setup|t3_ticket|wt_setup|ws_ticket|restore.*(db|database))\b'; then
        echo "t3-workspace"; return
    fi
    if echo "$lp" | grep -qE '\b(database|start (the )?backend|start (the )?frontend)\b'; then
        echo "t3-workspace"; return
    fi

    # No match
    echo ""
}

intent=$(detect_intent "$prompt")

# --- Supplementary skill detection (non-lifecycle, keyword-triggered) ---
# Domain/specialty skills loaded ALONGSIDE the lifecycle skill when the
# prompt contains keyword triggers. Mappings are read from a user config
# file so the hook stays generic (no private skill names in the public repo).
#
# Config file: $T3_SUPPLEMENTARY_SKILLS or ~/.teatree-skills.yml
# Format (simple YAML — one skill per top-level key, regex pattern as value):
#   my-ruff-skill: '\b(ruff|lint(er)? adopt)\b'
#   my-pdf-skill: '\b(acroform|pdf template)\b'
#
# Multiple skills can share patterns. Patterns are matched against the
# lowercase prompt via grep -qE.
_SUPP_CONFIG="${T3_SUPPLEMENTARY_SKILLS:-$HOME/.teatree-skills.yml}"
supplementary_skills=()
if [ -f "$_SUPP_CONFIG" ]; then
    lp_supp=$(echo "$prompt" | tr '[:upper:]' '[:lower:]')
    while IFS= read -r line; do
        # Skip comments and empty lines
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ "$line" =~ ^[[:space:]]*$ ]] && continue
        # Parse "skill-name: 'pattern'" or "skill-name: pattern"
        if [[ "$line" =~ ^([a-zA-Z][a-zA-Z0-9_-]+):[[:space:]]+(.*) ]]; then
            supp_skill="${BASH_REMATCH[1]}"
            supp_pattern="${BASH_REMATCH[2]}"
            # Strip surrounding quotes
            supp_pattern="${supp_pattern#\'}"; supp_pattern="${supp_pattern%\'}"
            supp_pattern="${supp_pattern#\"}"; supp_pattern="${supp_pattern%\"}"
            if echo "$lp_supp" | grep -qE "$supp_pattern" 2>/dev/null; then
                supplementary_skills+=("$supp_skill")
            fi
        fi
    done < "$_SUPP_CONFIG"
fi

# --- End-of-session detection: suggest t3-retro ---
# If no specific intent was detected, check for end-of-session patterns.
# Only suggest if: (a) at least one non-retro skill was loaded this session,
# (b) t3-retro is not already loaded, and (c) the prompt looks like a
# standalone wrap-up message (not "done with X" which triggers other skills).
if [ -z "$intent" ] && ! grep -qxF "t3-retro" "$skills_file" 2>/dev/null; then
    lp_retro=$(echo "$prompt" | tr '[:upper:]' '[:lower:]')
    # Match standalone end-of-session phrases (short prompts or end-of-line anchored)
    if echo "$lp_retro" | grep -qE '^\s*(done|all set|finished|all done|wrap up|that.s it|that.s all|ship it|we.re done|i.m done|looks good|lgtm)\s*[.!]?\s*$'; then
        # Check that at least one non-retro skill was loaded this session
        if [ -f "$skills_file" ] && grep -qE '^t3-' "$skills_file" 2>/dev/null && \
          grep -vqxF "t3-retro" "$skills_file" 2>/dev/null; then
            intent="t3-retro"
        fi
    fi
fi

# In project context with no specific intent → default to t3-code
# Most prompts in project context are about coding; t3-ticket is for explicit
# ticket intake (ticket numbers, "new ticket", "start working on").
if $project_context && [ -z "$intent" ]; then
    intent="t3-code"
fi

# No intent and no project context → stay silent
[ -z "$intent" ] && exit 0

# --- Check if the suggested skill is already loaded ---
is_loaded() {
    grep -qxF "$1" "$skills_file" 2>/dev/null
}

# --- Skill dependency resolution ---
# Parse "requires:" from SKILL.md YAML frontmatter to auto-include dependencies.
# This avoids wasting a round-trip where the LLM reads "Load /t3-workspace now"
# and then has to call the Skill tool again.
_get_skill_deps() {
    local skill_name="$1"
    local skill_md=""
    # Find the SKILL.md for this skill (check source repo first, then published)
    for candidate in \
        "$_SOURCE_ROOT/$skill_name/SKILL.md" \
        "$HOME/.agents/skills/$skill_name/SKILL.md" \
        "$HOME/.claude/skills/$skill_name/SKILL.md"; do
        if [ -f "$candidate" ]; then
            skill_md="$candidate"
            break
        fi
    done
    [ -z "$skill_md" ] && return

    # Parse "requires:" from YAML frontmatter (between --- markers)
    local in_frontmatter=false in_requires=false
    while IFS= read -r line; do
        if [[ "$line" == "---" ]]; then
            if $in_frontmatter; then break; fi
            in_frontmatter=true; continue
        fi
        $in_frontmatter || continue
        if [[ "$line" =~ ^requires: ]]; then
            in_requires=true; continue
        fi
        if [[ "$line" =~ ^[a-z] ]]; then
            in_requires=false; continue
        fi
        if $in_requires && [[ "$line" =~ ^[[:space:]]+-[[:space:]]+(.*) ]]; then
            local dep="${BASH_REMATCH[1]}"
            dep="${dep#\"}"; dep="${dep%\"}"
            echo "$dep"
        fi
    done < "$skill_md"
}

# Build the list of skills to suggest
suggest=()

# Always suggest t3-workspace as foundation if not loaded (all skills depend on it)
# Exception: t3-setup and t3-retro are standalone — they don't depend on t3-workspace
if [ "$intent" != "t3-setup" ] && [ "$intent" != "t3-retro" ] && ! is_loaded "t3-workspace"; then
    suggest+=("t3-workspace")
fi

# Suggest the detected intent skill if not loaded and not t3-workspace (already handled)
if [ -n "$intent" ] && [ "$intent" != "t3-workspace" ] && ! is_loaded "$intent"; then
    suggest+=("$intent")
fi

# Resolve dependencies of the intent skill (one level deep — no transitive resolution)
if [ -n "$intent" ]; then
    while IFS= read -r dep; do
        [ -z "$dep" ] && continue
        if ! is_loaded "$dep"; then
            # Avoid duplicates in suggest list
            already=false
            for existing in "${suggest[@]}"; do
                if [ "$existing" = "$dep" ]; then already=true; break; fi
            done
            $already || suggest+=("$dep")
        fi
    done < <(_get_skill_deps "$intent")
fi

# In project context, always suggest the overlay skill if not loaded
if $project_context && [ -n "$project_overlay" ] && ! is_loaded "$project_overlay"; then
    suggest+=("$project_overlay")
fi

# In project context, suggest companion skills (e.g., ac-django for backend repos)
# Parsed from companion_skills section of context-match.yml.
if $project_context && [ -n "$overlay_skill_dir" ]; then
    match_file="$overlay_skill_dir/hook-config/context-match.yml"
    if [ -f "$match_file" ]; then
        current_skill=""
        in_companion=false
        while IFS= read -r line; do
            [[ "$line" =~ ^[[:space:]]*# ]] && continue
            [[ "$line" =~ ^[[:space:]]*$ ]] && continue
            # Top-level key
            if [[ "$line" =~ ^[a-z] ]]; then
                if [[ "$line" =~ ^companion_skills: ]]; then
                    in_companion=true
                else
                    in_companion=false
                fi
                current_skill=""
                continue
            fi
            $in_companion || continue
            # Skill name key (2-space indent): "  ac-django:"
            if [[ "$line" =~ ^[[:space:]]{2}([a-z][a-z0-9_-]+): ]]; then
                current_skill="${BASH_REMATCH[1]}"
                continue
            fi
            # Pattern list item (4-space indent): "    - my-backend"
            if [ -n "$current_skill" ] && [[ "$line" =~ ^[[:space:]]+-[[:space:]]+(.*) ]]; then
                pat="${BASH_REMATCH[1]}"
                pat="${pat#\"}"; pat="${pat%\"}"
                matched=false
                if [[ "$PWD" == *"$pat"* ]]; then
                    matched=true
                elif [ -f "$active_file" ] && grep -qF "$pat" "$active_file" 2>/dev/null; then
                    matched=true
                fi
                if $matched && ! is_loaded "$current_skill"; then
                    suggest+=("$current_skill")
                    current_skill=""  # don't add same skill twice
                fi
            fi
        done < "$match_file"
    fi
fi

# Append supplementary (non-lifecycle) skills detected by keyword
for supp in "${supplementary_skills[@]}"; do
    [ -z "$supp" ] && continue
    if ! is_loaded "$supp"; then
        # Avoid duplicates in suggest list
        already=false
        for existing in "${suggest[@]}"; do
            if [ "$existing" = "$supp" ]; then already=true; break; fi
        done
        $already || suggest+=("$supp")
    fi
done

# Nothing to suggest → exit
[ ${#suggest[@]} -eq 0 ] && exit 0

# --- Build suggestion message ---
msg="LOAD THESE SKILLS NOW (call the Skill tool for each, before doing anything else): "

skill_list=""
for s in "${suggest[@]}"; do
    if [ -n "$skill_list" ]; then
        skill_list="${skill_list}, /${s}"
    else
        skill_list="/${s}"
    fi
done
msg="${msg}${skill_list}."

# --- Project context: inject reference paths from reference-injections.yml ---
if $project_context && [ -n "$overlay_skill_dir" ]; then
    injections_file="$overlay_skill_dir/hook-config/reference-injections.yml"
    if [ -f "$injections_file" ]; then
        # Parse YAML: extract "always" references for the detected skill.
        # Simple line-based parser (no external YAML lib needed).
        refs=""
        in_skill=false
        in_always=false
        while IFS= read -r line; do
            # Skip comments and empty lines
            [[ "$line" =~ ^[[:space:]]*# ]] && continue
            [[ "$line" =~ ^[[:space:]]*$ ]] && continue

            # Top-level skill key (no leading whitespace)
            if [[ "$line" =~ ^[a-z] ]]; then
                skill_key="${line%%:*}"
                if [ "$skill_key" = "$intent" ]; then
                    in_skill=true
                else
                    in_skill=false
                fi
                in_always=false
                continue
            fi

            $in_skill || continue

            # "always:" or "on-demand:" section
            if [[ "$line" =~ ^[[:space:]]+(always|on-demand): ]]; then
                section="${BASH_REMATCH[1]}"
                if [ "$section" = "always" ]; then
                    in_always=true
                else
                    in_always=false
                fi
                continue
            fi

            # List item under "always:"
            if $in_always && [[ "$line" =~ ^[[:space:]]+-[[:space:]]+(.*) ]]; then
                ref="${BASH_REMATCH[1]}"
                # Strip quotes
                ref="${ref#\"}"
                ref="${ref%\"}"
                if [ -n "$refs" ]; then
                    refs="${refs}, ${ref}"
                else
                    refs="${ref}"
                fi
            fi
        done < "$injections_file"

        if [ -n "$refs" ]; then
            overlay_label=$(echo "$project_overlay" | sed 's/^ac-//' | tr '[:lower:]' '[:upper:]')
            msg="${msg} ${overlay_label} references to read: ${refs}"
        fi
    fi
fi

# --- Session FSM transition ---
# Map the detected skill to a session phase and attempt a transition.
# This is advisory — a blocked gate just prints a warning, it doesn't
# prevent skill loading (the LLM still needs the skill to do its work).
_SESSION_DIR="${T3_SESSION_DIR:-/tmp/t3-sessions}"
mkdir -p "$_SESSION_DIR"
# Scope session to ticket dir (quality gates are per-ticket, not per-conversation)
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

target_phase=$(_skill_to_phase "$intent")
if [ -n "$target_phase" ]; then
    # Read current session state (lightweight — just parse JSON)
    current_phase="idle"
    visited="idle"
    if [ -f "$_session_file" ]; then
        current_phase=$(python3 -c "import json; d=json.load(open('$_session_file')); print(d.get('state','idle'))" 2>/dev/null || echo "idle")
        visited=$(python3 -c "import json; d=json.load(open('$_session_file')); print(' '.join(d.get('visited',[])))" 2>/dev/null || echo "idle")
    fi

    # Check quality gates (advisory warnings)
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

    # Update session state (always — gates are advisory in the hook)
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
