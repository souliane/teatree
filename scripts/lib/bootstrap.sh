#!/usr/bin/env bash
# bootstrap.sh — Thin bash wrapper for the t3 CLI
#
# Source this file from .zshrc:
#   source $T3_REPO/scripts/lib/bootstrap.sh
#
# Works in both bash and zsh.

export _T3_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")/.." && pwd)"

# XDG-compliant data directory for runtime state (ticket cache, MR reminders, dashboard)
export T3_DATA_DIR="${T3_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/teatree}"
[[ -d "$T3_DATA_DIR" ]] || mkdir -p "$T3_DATA_DIR"

# Extra directories to symlink from main repo into worktrees (comma-separated).
# Default: ".data" (database dumps/snapshots). Override in ~/.teatree if needed.
export T3_SHARED_DIRS="${T3_SHARED_DIRS:-.data}"

# Source shell helpers that must eval in caller's shell
source "$_T3_SCRIPTS_DIR/lib/shell_helpers.sh"

# Python runner — runs in a subshell cd'd to scripts dir so pyenv local
# picks up .python-version (3.12).  Exports _T3_ORIG_CWD so Python
# scripts can recover the caller's working directory.  Includes overlay
# scripts on PYTHONPATH when $T3_OVERLAY is set so project hooks are
# discovered by lib.init.
function _t3_python {
  local _pp="$_T3_SCRIPTS_DIR"
  [[ -n "${T3_OVERLAY:-}" && -d "$T3_OVERLAY/scripts" ]] && _pp="$T3_OVERLAY/scripts:$_pp"
  (export _T3_ORIG_CWD="$PWD"; cd "$_T3_SCRIPTS_DIR" && PYTHONPATH="$_pp" python3 "$@")
}

function _t3_py { _t3_python "$_T3_SCRIPTS_DIR/$1" "${@:2}"; }

# t3 — Unified entry point for all worktree lifecycle operations.
#
# Commands that modify the worktree environment (setup, db-refresh)
# trigger a direnv reload so env vars are picked up by the calling shell.
function t3 {
  _t3_py t3_cli.py "$@"
  local _rc=$?

  # Reload direnv after commands that modify .envrc / .env.worktree
  case "${1:-}" in
    lifecycle|db)
      _direnv_eval "$(pwd)"
      ;;
  esac

  return $_rc
}
