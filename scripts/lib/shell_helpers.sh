#!/usr/bin/env bash
# shell_helpers.sh — Generic functions that must eval in the caller's shell
#
# Sourced by _bootstrap.sh. These cannot be Python because they modify
# the parent shell's environment.

# Detect ticket dir from current shell state.
# Returns ticket dir path on stdout; exits non-zero when not in a ticket/worktree path.
function _detect_ticket_dir {
  local _td_env="${TICKET_DIR:-}"
  if [[ -n "$_td_env" && -d "$_td_env" ]]; then
    if [[ "$PWD" == "$_td_env" || "$PWD" == "$_td_env/"* ]]; then
      echo "$_td_env"
      return 0
    fi
  fi

  local _cwd="$PWD" _ws="${T3_WORKSPACE_DIR:-$HOME/workspace}"
  local _rel="${_cwd#$_ws/}"
  if [[ "$_rel" == "$_cwd" ]]; then
    return 1
  fi

  local _first="${_rel%%/*}"
  local _rest="${_rel#*/}"
  local _candidate="$_ws/$_first"
  if [[ "$_rest" == "$_rel" ]]; then
    if [[ -d "$_candidate" && ! -d "$_candidate/.git" ]]; then
      echo "$_candidate"
      return 0
    fi
    return 1
  fi

  if [[ -d "$_candidate" && ! -d "$_candidate/.git" ]]; then
    echo "$_candidate"
    return 0
  fi

  return 1
}

# Source env file with auto-export when present.
function _source_env_file {
  local _path="$1"
  [[ -f "$_path" ]] || return 0
  set -a
  source "$_path"
  set +a
}

# Detect current shell and eval direnv export.
# Usage: _direnv_eval <directory>
function _direnv_eval {
  local shell_name
  if [[ -n "$ZSH_VERSION" ]]; then
    shell_name=zsh
  elif [[ -n "$BASH_VERSION" ]]; then
    shell_name=bash
  else
    shell_name=bash
  fi
  command -v direnv &>/dev/null || return 0
  eval "$(cd "$1" && direnv export "$shell_name")"
}
