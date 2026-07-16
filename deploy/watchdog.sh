#!/usr/bin/env bash
# teatree in-daemon self-heal watchdog (owner directive #10).
#
# Runs as a sidecar CONTAINER inside the compose stack (service teatree-watchdog,
# restart: always, no depends_on) so a FULL stack outage — the init crash-loop
# that froze the factory for 7h, where the worker WAS the monitor and died with
# the alerting — is detected and repaired by something the Docker daemon keeps
# alive independently. The daemon is the only supervisor present on BOTH Linux
# and macOS, so this replaces the Linux-only systemd timer (#3289) with a
# cross-platform mechanism. With `--loop` the container drives its own cadence;
# the default single pass is on-demand/test-friendly. Each pass:
#
#   1. `docker compose -p teatree up -d --no-recreate` — restart anything that
#      went down. Gated on init state: a completed one-shot init (exited 0) is
#      EXCLUDED (an empirical fact — `up -d --no-recreate` re-runs a completed
#      init every pass, which would replay the heavy ~minute init on every tick),
#      while a missing/failed init IS included so the init-failure outage recovers.
#   2. `t3 doctor check --json` inside a live container — read the factory health,
#      including the H24 self-heal detectors (dead containers, a free worker flock
#      over overdue loop work, stranded headless tasks, stale timers, unrunnable
#      interactive tasks, failed tasks on live tickets, a drifted runtime clone).
#   3. On any red finding, DM the owner via `t3 teatree notify send`, keyed on the
#      finding set so an ongoing outage does not re-spam every pass.
#
# Safe by construction: the ONLY mutating docker op is `up -d --no-recreate`
# (idempotent, never destructive, never recreates a running container). The
# watchdog never prunes, removes, stops, or recreates anything.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="${BASH_SOURCE[0]}"
COMPOSE_FILE="${TEATREE_WATCHDOG_COMPOSE_FILE:-$SCRIPT_DIR/docker-compose.yml}"
PROJECT="${TEATREE_WATCHDOG_PROJECT:-teatree}"
OVERLAY="${TEATREE_WATCHDOG_OVERLAY:-teatree}"
INTERVAL="${TEATREE_WATCHDOG_INTERVAL:-300}"
PASS_TIMEOUT="${TEATREE_WATCHDOG_PASS_TIMEOUT:-300}"
INIT_SERVICE="${TEATREE_WATCHDOG_INIT_SERVICE:-teatree-init}"
# Services to `exec` the read commands in (first reachable one wins).
EXEC_SERVICES="${TEATREE_WATCHDOG_EXEC_SERVICES:-teatree-admin teatree-worker}"
# Services restarted when init has already completed (init excluded — see header).
read -ra APP_SERVICES <<<"${TEATREE_WATCHDOG_APP_SERVICES:-teatree-worker teatree-admin teatree-slack-listener teatree-watchdog}"

log() { printf '%s watchdog: %s\n' "$(date -uIseconds)" "$*" >&2; }

compose() { docker compose -p "$PROJECT" -f "$COMPOSE_FILE" "$@"; }

# Echo the init service's compose state as "<State> <ExitCode>" (e.g. "exited 0"),
# or empty when it cannot be determined (never created, docker unreachable, jq
# absent). An empty result routes to the full `up -d` — the safe default that
# creates or re-runs init.
init_state() {
  local json
  json="$(compose ps -a --format json "$INIT_SERVICE" 2>/dev/null)" || return 0
  [ -n "$json" ] || return 0
  printf '%s\n' "$json" | jq -rs 'if length > 0 then "\(.[0].State) \(.[0].ExitCode)" else empty end' 2>/dev/null
}

# Restart anything that went down, gated on init state (see header rationale).
restart_down_services() {
  local state
  state="$(init_state)"
  if [ "$state" = "exited 0" ]; then
    log "init complete (exited 0) — restarting app services only: ${APP_SERVICES[*]}"
    compose up -d --no-recreate --no-deps "${APP_SERVICES[@]}"
  else
    log "init not complete (state='${state:-unknown}') — full up -d --no-recreate"
    compose up -d --no-recreate
  fi
}

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

run_pass() {
  log "restarting any down services (gated on init state)"
  if ! restart_down_services; then
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

# Drive the cadence in-container: one bounded pass per interval, forever. A failed
# or timed-out pass must never kill the loop (that would silently retire the only
# supervisor), so the pass is wrapped in `timeout` and its failure is logged, not
# fatal. Each pass re-invokes this script in its default single-pass mode.
run_loop() {
  log "watchdog loop starting (interval=${INTERVAL}s, pass timeout=${PASS_TIMEOUT}s)"
  while :; do
    timeout "$PASS_TIMEOUT" bash "$SELF" || log "pass failed or timed out (rc=$?)"
    sleep "$INTERVAL"
  done
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

# A short, stable digest of the body so an unchanged outage reuses one key.
_stable_key() {
  if command -v sha1sum >/dev/null 2>&1; then
    sha1sum | cut -c1-16
  else
    cksum | cut -d' ' -f1
  fi
}

if [ "${1:-}" = "--loop" ]; then
  run_loop
else
  run_pass
fi
