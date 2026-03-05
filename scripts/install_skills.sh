#!/usr/bin/env bash
# install_skills.sh — Symlink teatree skills into detected agent runtimes.
#
# Contributor install only: points agent runtimes at the live git clone so
# teatree self-improvement edits land in version-controlled files immediately.
#
# Consumer installs from remote repos can still use `npx skills add`, but that
# managed install does not point at your existing git clone.

set -euo pipefail

T3_ROOT="${1:-}"
if [[ -z "$T3_ROOT" ]]; then
  T3_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fi
T3_ROOT="${T3_ROOT/#\~/$HOME}"

if [[ ! -d "$T3_ROOT" ]]; then
  echo "ERROR: teatree root not found at $T3_ROOT" >&2
  exit 1
fi

origin_url="$(git -C "$T3_ROOT" remote get-url origin 2>/dev/null || true)"
if [[ -n "$origin_url" && ! "$origin_url" =~ teatree(\.git)?$ ]]; then
  echo "WARNING: T3_REPO origin ($origin_url) does not look like a teatree fork."
  echo "         Skill files are agent instructions — verify you trust this source."
  echo ""
fi

UNIVERSAL_AGENT_HINTS=(
  "$HOME/.agents"
  "$HOME/.claude"
  "$HOME/.codex"
  "$HOME/.cursor"
  "$HOME/.copilot"
  "$HOME/Library/Application Support/Cursor"
  "$HOME/Library/Application Support/Code/User/globalStorage/github.copilot-chat"
  "$HOME/.config/Code/User/globalStorage/github.copilot-chat"
)

has_any_hint() {
  local hint
  for hint in "$@"; do
    [[ -e "$hint" ]] && return 0
  done
  return 1
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

TARGET_ROOTS=()
if has_any_hint "${UNIVERSAL_AGENT_HINTS[@]}" || command_exists claude || command_exists codex; then
  TARGET_ROOTS+=("$HOME/.agents/skills")
fi
if [[ -d "$HOME/.claude" ]] || command_exists claude; then
  TARGET_ROOTS+=("$HOME/.claude/skills")
fi
if [[ -d "$HOME/.codex" ]] || command_exists codex; then
  TARGET_ROOTS+=("$HOME/.codex/skills")
fi
if [[ -d "$HOME/.cursor" ]] || command_exists cursor; then
  TARGET_ROOTS+=("$HOME/.cursor/skills")
fi
if [[ -d "$HOME/.copilot" ]] || [[ -d "$HOME/Library/Application Support/Code/User/globalStorage/github.copilot-chat" ]] || [[ -d "$HOME/.config/Code/User/globalStorage/github.copilot-chat" ]]; then
  TARGET_ROOTS+=("$HOME/.copilot/skills")
fi

if [[ ${#TARGET_ROOTS[@]} -eq 0 ]]; then
  echo "No supported agent runtime detected. Checked ~/.agents, ~/.claude, ~/.codex, ~/.cursor, ~/.copilot, Cursor, and GitHub Copilot storage." >&2
  echo "Nothing installed." >&2
  exit 0
fi

SKILL_NAMES=()
SKILL_DIRS=()
IS_CONTAINER=()

while IFS= read -r skill_md; do
  skill_dir="$(dirname "$skill_md")"
  SKILL_NAMES+=("$(basename "$skill_dir")")
  SKILL_DIRS+=("$skill_dir")
  IS_CONTAINER+=("no")
done < <(command find "$T3_ROOT" -name "SKILL.md" -type f 2>/dev/null | sort)

SKILL_NAMES+=("teatree")
SKILL_DIRS+=("$T3_ROOT")
IS_CONTAINER+=("yes")

linked=0
created=0
skipped=0
warnings=0

has_local_modifications() {
  local copy_dir="$1"
  local source_dir="$2"
  while IFS= read -r -d '' file; do
    local rel="${file#$copy_dir/}"
    local src_file="$source_dir/$rel"
    if [[ ! -f "$src_file" ]]; then
      return 0
    fi
    if ! command diff -q "$file" "$src_file" >/dev/null 2>&1; then
      return 0
    fi
  done < <(command find "$copy_dir" -type f -not -path '*/__pycache__/*' -not -name '*.pyc' -print0)
  return 1
}

i=0
while [[ $i -lt ${#SKILL_NAMES[@]} ]]; do
  skill="${SKILL_NAMES[$i]}"
  source_path="${SKILL_DIRS[$i]}"
  is_container="${IS_CONTAINER[$i]}"
  i=$((i + 1))

  [[ -d "$source_path" ]] || {
    echo "  SKIP  $skill — source not found at $source_path"
    skipped=$((skipped + 1))
    continue
  }

  for target_root in "${TARGET_ROOTS[@]}"; do
    if [[ "$is_container" == "yes" && "$target_root" != "$HOME/.agents/skills" ]]; then
      continue
    fi

    target="$target_root/$skill"
    command mkdir -p "$target_root"

    if [[ -L "$target" ]]; then
      current_target="$(command readlink "$target")"
      if [[ "$current_target" == "$source_path" ]]; then
        continue
      fi
      command rm "$target"
      command ln -s "$source_path" "$target"
      echo "  FIXED $target -> $source_path (was: $current_target)"
      linked=$((linked + 1))
      continue
    fi

    if [[ -d "$target" ]]; then
      if has_local_modifications "$target" "$source_path"; then
        echo "  WARN  $target has local modifications — skipping (diff manually)"
        warnings=$((warnings + 1))
        continue
      fi
      command rm -rf "$target"
      command ln -s "$source_path" "$target"
      echo "  LINK  $target -> $source_path (replaced managed copy)"
      linked=$((linked + 1))
      continue
    fi

    if [[ ! -e "$target" ]]; then
      command ln -s "$source_path" "$target"
      echo "  NEW   $target -> $source_path"
      created=$((created + 1))
    fi
  done
done

echo ""
echo "Done: $linked fixed, $created new, $skipped skipped, $warnings warnings"
if [[ $warnings -gt 0 ]]; then
  echo "  WARNING: Review warned skills manually: diff <copy> <source>"
fi
