#!/usr/bin/env bash
# bootstrap.sh — Thin bash wrappers that delegate to Python
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

# Extension-point caller — delegates to _registry.call()
function _t3_delegate {
  _t3_python -c "import sys; import lib.init; lib.init.init(); from lib.registry import call; call('$1', *sys.argv[1:])" "${@:2}"
}

# ============================================================
# User-facing wrappers
# ============================================================

function t3_ticket { _t3_py ws_ticket.py "$@"; }

function t3_setup {
  _t3_py wt_setup.py "$@" && _direnv_eval "$(pwd)"
}

function t3_db_refresh {
  _t3_py wt_db_refresh.py "$@" && _direnv_eval "$(pwd)"
}

function t3_finalize { _t3_py wt_finalize.py "$@"; }

function t3_clean { _t3_py git_clean_them_all.py "$@"; }

# Delegate wrappers (extension points)
function t3_backend { _t3_delegate wt_run_backend "$@"; }
function t3_frontend { _t3_delegate wt_run_frontend "$@"; }
function t3_build_frontend { _t3_delegate wt_build_frontend "$@"; }
function t3_tests { _t3_delegate wt_run_tests "$@"; }

function t3_restore_ci_db { _t3_delegate wt_restore_ci_db "$@"; }
function t3_reset_passwords { _t3_delegate wt_reset_passwords "$@"; }
function t3_trigger_e2e { _t3_delegate wt_trigger_e2e "$@"; }
function t3_quality_check { _t3_delegate wt_quality_check "$@"; }
function t3_fetch_ci_errors { _t3_delegate wt_fetch_ci_errors "$@"; }
function t3_fetch_failed_tests { _t3_delegate wt_fetch_failed_tests "$@"; }

# Utility wrappers (no extension points — always use teatree scripts directly)
function t3_fetch_issue { _t3_py fetch_issue_context.py "$@"; }
function t3_cancel_pipelines { _t3_py cancel_stale_pipelines.py "$@"; }
function t3_create_mr { _t3_py create_mr.py "$@"; }
function t3_detect_tenant { _t3_py detect_tenant.py "$@"; }
function t3_check_gates { _t3_py check_transition_gates.py "$@"; }
function t3_verify_services { _t3_py verify_services.py "$@"; }
function t3_collect_followup { _t3_py collect_followup_data.py "$@"; }

function t3_start {
  echo "t3_start: not configured. Source a project skill bootstrap first."
}
