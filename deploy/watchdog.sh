#!/usr/bin/env bash
# teatree external self-heal watchdog (owner directive #10).
#
# Runs OUTSIDE the compose stack on a fixed cadence (systemd timer), so a FULL
# stack outage — the init crash-loop that froze the factory for 7h, where the
# worker WAS the monitor and died with the alerting — is detected and repaired
# by something that is still alive. Each pass:
#
#   1. `docker compose -p teatree up -d`  — restart anything that went down.
#   2. `t3 doctor check --json` inside a live container — read the factory health,
#      including the H24 self-heal detectors (dead containers, a free worker flock
#      over overdue loop work, stranded headless tasks, stale timers, unrunnable
#      interactive tasks, failed tasks on live tickets, a drifted runtime clone).
#   3. On any red finding, DM the owner via `t3 teatree notify send`, keyed on the
#      finding set so an ongoing outage does not re-spam every pass.
#
# Safe by construction: the ONLY mutating docker op is `up -d` (idempotent, never
# destructive). The watchdog never prunes, removes, stops, or recreates anything.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${TEATREE_WATCHDOG_COMPOSE_FILE:-$SCRIPT_DIR/docker-compose.yml}"
PROJECT="${TEATREE_WATCHDOG_PROJECT:-teatree}"
OVERLAY="${TEATREE_WATCHDOG_OVERLAY:-teatree}"
# Services to `exec` the read commands in (first reachable one wins).
EXEC_SERVICES="${TEATREE_WATCHDOG_EXEC_SERVICES:-teatree-admin teatree-worker}"

log() { printf '%s watchdog: %s\n' "$(date -uIseconds)" "$*" >&2; }

compose() { docker compose -p "$PROJECT" -f "$COMPOSE_FILE" "$@"; }

# Run a command inside the first reachable exec service. Echoes its stdout; returns
# the command's exit status, or 125 when no service could be reached.
exec_in_stack() {
  local svc
  for svc in $EXEC_SERVICES; do
    if compose exec -T "$svc" "$@"; then
      return 0
    fi
  done
  return 125
}

# Send the owner DM (body on stdin). Never aborts the watchdog: an unwired Slack
# box (the default deploy provisions no Slack credential) just logs and continues.
notify_owner() {
  local key="$1"
  if exec_in_stack t3 "$OVERLAY" notify send - --idempotency-key "$key" >/dev/null; then
    log "owner DMed (key=$key)"
  else
    log "could not deliver owner DM (Slack may be unwired on this box)"
  fi
}

main() {
  log "restarting any down services: docker compose -p $PROJECT up -d"
  if ! compose up -d; then
    # The stack could not even be brought up — the strongest outage signal.
    printf 'teatree watchdog: `docker compose up -d` FAILED on the box — the stack is DOWN and could not be restarted. SSH in and inspect `docker compose -p %s logs`.' "$PROJECT" \
      | notify_owner "watchdog:compose-up-failed:$(date -u +%Y%m%d%H)"
    return 0
  fi

  local raw
  if ! raw="$(exec_in_stack t3 doctor check --json)"; then
    printf 'teatree watchdog: could not run `t3 doctor` in any service (%s) — the stack is unreachable even after `up -d`. SSH in and inspect `docker compose -p %s ps`.' "$EXEC_SERVICES" "$PROJECT" \
      | notify_owner "watchdog:doctor-unreachable:$(date -u +%Y%m%d%H)"
    return 0
  fi

  # Keep only the JSON line (doctor may print incidental lines before it).
  local json
  json="$(printf '%s\n' "$raw" | grep '"ok"' | tail -n 1)"
  if [ -z "$json" ]; then
    log "doctor produced no JSON verdict — treating as healthy"
    return 0
  fi

  case "$json" in
    *'"ok": true'*)
      log "doctor: all green"
      return 0
      ;;
  esac

  # Red: build the DM body (FAIL findings) and a stable idempotency key from them.
  local body key
  body="$(printf '%s' "$json" | _extract_fail_body)"
  key="watchdog:red:$(printf '%s' "$body" | _stable_key)"
  log "doctor RED — DMing owner"
  printf 'teatree watchdog found red findings on the box:\n\n%s\n\nThe stack was already `up -d`-restarted this pass; SSH in if it persists.' "$body" \
    | notify_owner "$key"
}

# Extract the FAIL messages from the doctor JSON. Uses python3 (present on the box)
# and degrades to a generic body when it is absent.
_extract_fail_body() {
  if command -v python3 >/dev/null 2>&1; then
    python3 - <<'PY'
import json, sys
try:
    data = json.load(sys.stdin)
    fails = [f["message"] for f in data.get("findings", []) if f.get("level") == "FAIL"]
except Exception:
    fails = []
print("\n".join(f"- {m}" for m in fails) if fails else "- (see `t3 doctor check` on the box for detail)")
PY
  else
    printf '%s' "- one or more red findings (install python3 on the box for detail, or run \`t3 doctor check\`)"
  fi
}

# A short, stable digest of the body so an unchanged outage re-uses one key.
_stable_key() {
  if command -v sha1sum >/dev/null 2>&1; then
    sha1sum | cut -c1-16
  else
    cksum | cut -d' ' -f1
  fi
}

main "$@"
