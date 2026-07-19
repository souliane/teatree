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

# Gather the compose container states from the daemon socket and base64-encode them
# for handoff to `t3 doctor`. This watchdog is the ONLY container with
# /var/run/docker.sock, while `t3 doctor` runs in a socket-less app container whose
# own `docker ps` cannot reach the daemon — so without this handoff the doctor's
# compose-stack detector (crash-looping init / down worker) silently passes every
# real outage. Empty on any docker/base64 failure — the doctor then degrades to a
# pass exactly as it did before this handoff, and its other detectors still run.
compose_states_b64() {
  local ps
  ps="$(docker ps --all \
        --filter "label=com.docker.compose.project=$PROJECT" \
        --format '{{.Label "com.docker.compose.service"}}'$'\t''{{.State}}'$'\t''{{.Status}}' 2>/dev/null)" || return 0
  printf '%s' "$ps" | base64 -w0 2>/dev/null || true
}

# Run `t3 doctor check --json` in the first REACHABLE exec service, capturing its
# stdout into DOCTOR_RAW regardless of doctor's exit code. This is the heart of
# the #3440 fix: a red-findings verdict exits NON-ZERO yet is a healthy RUN of
# doctor, so the watchdog must NOT read a non-zero exit as "unreachable" (that
# made the red-findings DM path below dead code). Reachability is probed
# separately (a trivial `exec ... true`); only a genuinely unreachable service
# falls through to the next one. Returns 0 when a service was reached (DOCTOR_RAW
# set, possibly empty), 125 when NO exec service could be reached at all.
run_doctor() {
  local svc states_b64
  DOCTOR_RAW=""
  states_b64="$(compose_states_b64)"
  for svc in $EXEC_SERVICES; do
    if compose exec -T "$svc" true >/dev/null 2>&1; then
      # `|| true`: doctor exits non-zero on red findings; keep its stdout, drop
      # the exit code (set -e must not abort, and the code is NOT the signal).
      # `-e TEATREE_DOCTOR_COMPOSE_PS`: hand the socket-only container states to the
      # doctor's compose-stack detector, which cannot reach the daemon itself.
      DOCTOR_RAW="$(compose exec -T -e "TEATREE_DOCTOR_COMPOSE_PS=$states_b64" "$svc" t3 doctor check --json 2>/dev/null || true)"
      return 0
    fi
  done
  return 125
}

# The three hard-outage alarms below key on a DAILY bucket (`%Y%m%d`), not an
# hourly one: `notify_user` dedups on the key, so an hourly bucket re-DM'd the
# identical "stack down" alarm every hour (13+ overnight copies observed). A
# daily bucket collapses a persisting unchanged outage to at most one DM/day
# while still re-alerting each day it persists and on a next-day recurrence.
run_pass() {
  log "restarting any down services (gated on init state)"
  if ! restart_down_services; then
    # The stack could not even be brought up — the strongest outage signal.
    printf 'teatree watchdog: `docker compose up -d` FAILED on the box — the stack is DOWN and could not be restarted. SSH in and inspect `docker compose -p %s logs`.' "$PROJECT" \
      | notify_owner "watchdog:compose-up-failed:$(date -u +%Y%m%d)"
    return 0
  fi

  if ! run_doctor; then
    # No exec service could be reached at all — a genuine transport failure, the
    # ONLY case that is truly "unreachable" (distinct from doctor running and
    # returning a red verdict, which is handled below).
    printf 'teatree watchdog: could not exec `t3 doctor` in any service (%s) — the stack is unreachable even after `up -d`. SSH in and inspect `docker compose -p %s ps`.' "$EXEC_SERVICES" "$PROJECT" \
      | notify_owner "watchdog:doctor-unreachable:$(date -u +%Y%m%d)"
    return 0
  fi

  # Branch on the PRESENCE of a parseable JSON verdict, NOT on doctor's exit code
  # (#3440). Keep only the JSON line (doctor may print incidental lines before it).
  # `|| true`: no match makes the pipeline exit non-zero under `set -o pipefail`,
  # which would abort here before the no-verdict branch could fire.
  local json
  json="$(printf '%s\n' "$DOCTOR_RAW" | grep '"ok"' | tail -n 1 || true)"
  if [ -z "$json" ]; then
    # Doctor was reachable but emitted no parseable verdict: a half-crashed doctor
    # is itself a RED condition, not a healthy pass (the old code treated it as
    # healthy and stayed silent). DM at most once per day so a persistent breakage is seen.
    log "doctor reachable but produced no JSON verdict — treating as RED"
    printf 'teatree watchdog: `t3 doctor check --json` ran but produced NO parseable verdict — doctor may be crashing on the box. SSH in and run `t3 doctor check` in `docker compose -p %s exec teatree-admin`.' "$PROJECT" \
      | notify_owner "watchdog:doctor-no-verdict:$(date -u +%Y%m%d)"
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

# Extract the FAIL messages from the doctor JSON (read on stdin). Uses python3
# (present on the box) and degrades to a generic body when it is absent. Uses
# `python3 -c` rather than a `-` heredoc: a `python3 - <<'PY'` feeds the heredoc
# as the PROGRAM on stdin, leaving `sys.stdin` at EOF so the piped verdict is
# never read — the body would always be the generic fallback.
_extract_fail_body() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    fails = [f["message"] for f in data.get("findings", []) if f.get("level") == "FAIL"]
except Exception:
    fails = []
print("\n".join(f"- {m}" for m in fails) if fails else "- (see `t3 doctor check` on the box for detail)")
'
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

# Run the dispatch only when EXECUTED, not when sourced — so a test can source
# this file and drive `run_pass` / `run_doctor` in isolation with stubbed docker.
# `run_loop` re-invokes the script with `bash "$SELF"`, which is an execution, so
# the default single pass still fires there.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  if [ "${1:-}" = "--loop" ]; then
    run_loop
  else
    run_pass
  fi
fi
