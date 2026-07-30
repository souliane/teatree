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
#   2. Announce the repair. State is sampled BEFORE the `up -d` and re-read after, so
#      every service the watchdog had to bring back is DMed with how long it had been
#      gone (the liveness ledger below). A silent auto-heal is indistinguishable from
#      a healthy idle factory — that is how a 4h worker outage reached the owner only
#      because they happened to open the dashboard. Gated on deploy-awareness too.
#   3. `t3 doctor check --json` inside the WORKER (the container sized for heavy
#      work; see EXEC_SERVICES) — read the factory health,
#      including the H24 self-heal detectors (dead containers, a free worker flock
#      over overdue loop work, stranded headless tasks, stale timers, unrunnable
#      interactive tasks, failed tasks on live tickets, a drifted runtime clone).
#   4. On any red finding, DM the owner via `t3 teatree notify send`, keyed on the
#      finding set so an ongoing outage does not re-spam every pass. Three of those
#      findings — a free worker flock, a down slack-listener, a clone behind
#      origin — are what a ROLLING DEPLOY looks like mid-swap, so they are gated on
#      the deploy-awareness block below; every other finding pages as before.
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
# Services to `exec` the read commands in (first reachable one wins). The WORKER
# leads (#3651): `t3 doctor check --json` boots Django, scans the DB and makes live
# third-party HTTP round-trips — measured at 380 MiB peak, which under the admin's
# then-512m cap (257 MiB idle just serving the dashboard) left ~130 MiB of headroom
# and restarted the admin on every pass. The admin cap is now 2g, but the worker is
# still the container sized for heavy work, so nothing recurring competes with
# gunicorn; the admin stays the fallback so a down worker never blinds the watchdog.
EXEC_SERVICES="${TEATREE_WATCHDOG_EXEC_SERVICES:-teatree-worker teatree-admin}"
# Bounded retry for a probe that could not RUN because its target was mid-restart or
# not yet up — a transient, distinct from a completed run that returned no verdict.
DOCTOR_RETRIES="${TEATREE_WATCHDOG_DOCTOR_RETRIES:-3}"
DOCTOR_RETRY_DELAY="${TEATREE_WATCHDOG_DOCTOR_RETRY_DELAY:-15}"
# Services restarted when init has already completed (init excluded — see header).
read -ra APP_SERVICES <<<"${TEATREE_WATCHDOG_APP_SERVICES:-teatree-worker teatree-admin teatree-slack-listener teatree-watchdog}"

# Stale-temp trim: services to sweep, the disk temp roots to sweep in each, and the
# minimum age (minutes) a scratch entry must reach before it is trimmed. Runtime
# temp is routed to disk (/var/tmp) by the entrypoint + settings template, but a
# crashed/abandoned run can still leave pytest/uv/claude scratch behind that grows
# unbounded over weeks — the periodic half of the tmpfs-fill guard.
TEMP_TRIM_SERVICES="${TEATREE_WATCHDOG_TEMP_TRIM_SERVICES:-teatree-admin teatree-worker}"
TEMP_TRIM_ROOTS="${TEATREE_WATCHDOG_TEMP_TRIM_ROOTS:-/var/tmp /tmp}"
TEMP_TRIM_MIN_AGE_MIN="${TEATREE_WATCHDOG_TEMP_TRIM_MIN_AGE_MIN:-720}"

# Deploy-awareness (#3732). deploy.sh holds this host flock for the whole
# convergence (path-identity mounted read-only into this container, see
# docker-compose.yml); the recreate window is the grace after a container was
# CREATED, one watchdog interval by default; the pending-state file carries the
# two-strikes ledger across passes.
DEPLOY_LOCK="${TEATREE_WATCHDOG_DEPLOY_LOCK:-${TEATREE_DEPLOY_LOCK:-/tmp/teatree-deploy.lock}}"
DEPLOY_RECREATE_WINDOW="${TEATREE_WATCHDOG_DEPLOY_RECREATE_WINDOW:-$INTERVAL}"
DEPLOY_PENDING_STATE="${TEATREE_WATCHDOG_DEPLOY_PENDING_STATE:-/var/tmp/teatree-watchdog-deploy-sensitive.state}"

# Re-surface ledger: "<episode> <digest>" of the LAST observed red finding set. The
# episode counts green→red transitions, so a finding set that CLEARS and later returns
# is a new incident rather than a repeat of its own pre-clear key.
RED_STATE="${TEATREE_WATCHDOG_RED_STATE:-/var/tmp/teatree-watchdog-red.state}"

# Liveness ledger: "<service> <epoch-last-seen-running>" per line. It exists so "inactive
# since when" is answerable from the DM itself rather than from `docker inspect` after
# the fact.
LIVENESS_STATE="${TEATREE_WATCHDOG_LIVENESS_STATE:-/var/tmp/teatree-watchdog-liveness.state}"

log() { printf '%s watchdog: %s\n' "$(date -uIseconds)" "$*" >&2; }

# The UTC day the DM keys bucket on — the deliberate long re-surface interval. Overridable
# so the multi-day re-surface behaviour is testable without waiting a day.
day_bucket() { printf '%s' "${TEATREE_WATCHDOG_DAY_BUCKET:-$(date -u +%Y%m%d)}"; }

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
  printf '%s' "$(compose_service_states)" | base64 -w0 2>/dev/null || true
}

# One `docker ps` shape for the whole script: `<service><TAB><State><TAB><Status>` rows
# for this compose project. Both consumers — the doctor handoff above and the liveness
# ledger below — read it, so a format change can never desync them. Empty on any docker
# failure, which every consumer treats as "cannot tell", never as "everything is up".
compose_service_states() {
  docker ps --all \
    --filter "label=com.docker.compose.project=$PROJECT" \
    --format '{{.Label "com.docker.compose.service"}}'$'\t''{{.State}}'$'\t''{{.Status}}' 2>/dev/null || true
}

# The APP_SERVICES the daemon does NOT report as `running`, one per line — a service
# with no container at all counts as down. Yields NOTHING when the daemon could not be
# read: the caller must not turn an unreadable socket into "the stack is healthy".
down_app_services() {
  local states svc state
  states="$(compose_service_states)"
  [ -n "$states" ] || return 0
  for svc in "${APP_SERVICES[@]}"; do
    state="$(printf '%s\n' "$states" | awk -F'\t' -v s="$svc" '$1 == s { print $2; exit }')"
    [ "$state" = running ] || printf '%s\n' "$svc"
  done
}

# Epoch this service was last OBSERVED running, empty when the ledger has never seen it.
# `|| true` because the whole script runs under `set -e` and an absent ledger (the first
# pass on a fresh box) makes awk exit non-zero — which would abort the pass.
_last_seen_running() {
  awk -v s="$1" '$1 == s { print $2; exit }' "$LIVENESS_STATE" 2>/dev/null || true
}

# Stamp every running service at $1 and carry each down service's previous stamp forward
# — that carried stamp IS the "inactive since when" the owner had to read `docker inspect`
# for. Best-effort like the other ledgers: losing it costs the duration in one DM, never
# the supervisor.
_record_liveness() {
  local now="$1" down="$2" svc rows=""
  for svc in "${APP_SERVICES[@]}"; do
    if printf '%s\n' "$down" | grep -qxF "$svc"; then
      rows="$rows$svc $(_last_seen_running "$svc")"$'\n'
    else
      rows="$rows$svc $now"$'\n'
    fi
  done
  printf '%s' "$rows" >"$LIVENESS_STATE" 2>/dev/null ||
    log "could not persist the liveness ledger at $LIVENESS_STATE"
}

_downtime_phrase() {
  local last
  last="$(_last_seen_running "$1")"
  if [ -z "$last" ]; then
    printf 'down for an unknown period'
  else
    printf 'down ~%s min' "$(((${2} - last) / 60))"
  fi
}

# DM the owner naming every service that was DOWN before this pass restarted it, and for
# how long it had been gone. The restart IS the news: a silent auto-heal that took hours
# is indistinguishable from a healthy idle factory, which is exactly how a 4h outage
# reached the owner only because they happened to look at the dashboard. Verified by
# re-read — the post-restart state decides whether each service is reported recovered or
# STILL DOWN, so the DM never claims a repair that did not land. Skipped while a
# convergence is in flight: a rolling swap legitimately stops containers.
announce_repaired_services() {
  local down="$1" still_down="$2" now="$3" body="" svc
  [ -n "$down" ] || return 0
  if deploy_in_flight; then
    log "deploy in flight — not paging for the services this pass restarted"
    return 0
  fi
  while IFS= read -r svc; do
    [ -n "$svc" ] || continue
    if printf '%s\n' "$still_down" | grep -qxF "$svc"; then
      body="$body- $svc ($(_downtime_phrase "$svc" "$now")) — STILL DOWN after the restart"$'\n'
    else
      body="$body- $svc ($(_downtime_phrase "$svc" "$now")) — restarted"$'\n'
    fi
  done <<<"$down"
  log "watchdog repaired down services — DMing owner"
  printf 'teatree watchdog found stack services DOWN and ran `up -d`:\n\n%s\nA silent auto-heal is indistinguishable from an idle factory, so the restart itself is reported. Nothing else was changed.' "$body" |
    notify_owner "watchdog:repaired:$(printf '%s' "$down" | _stable_key):$(day_bucket)"
}

# Run `t3 doctor check --json` in the first REACHABLE exec service, capturing its
# stdout into DOCTOR_RAW regardless of doctor's exit code. This is the heart of
# the #3440 fix: a red-findings verdict exits NON-ZERO yet is a healthy RUN of
# doctor, so the watchdog must NOT read a non-zero exit as "unreachable" (that
# made the red-findings DM path below dead code). Reachability is probed
# separately (a trivial `exec ... true`); only a genuinely unreachable service
# falls through to the next one. Returns 0 when a service was reached (DOCTOR_RAW
# set, possibly empty), 125 when NO exec service could be reached at all. Either
# way DOCTOR_ERR carries the daemon's stderr, which run_doctor classifies.
_doctor_attempt() {
  local svc states_b64 err_file probe_err
  DOCTOR_RAW=""
  DOCTOR_ERR=""
  states_b64="$(compose_states_b64)"
  err_file="$(mktemp)"
  for svc in $EXEC_SERVICES; do
    probe_err="$(compose exec -T "$svc" true 2>&1 >/dev/null)" && {
      # `|| true`: doctor exits non-zero on red findings; keep its stdout, drop
      # the exit code (set -e must not abort, and the code is NOT the signal).
      # `-e TEATREE_DOCTOR_COMPOSE_PS`: hand the socket-only container states to the
      # doctor's compose-stack detector, which cannot reach the daemon itself.
      DOCTOR_RAW="$(compose exec -T -e "TEATREE_DOCTOR_COMPOSE_PS=$states_b64" "$svc" t3 doctor check --json 2>"$err_file" || true)"
      DOCTOR_ERR="$(cat "$err_file")"
      rm -f "$err_file"
      return 0
    }
    DOCTOR_ERR="$DOCTOR_ERR$probe_err"$'\n'
  done
  rm -f "$err_file"
  return 125
}

# A daemon refusal to exec into a container that is restarting / not (yet) running.
# This is the target being momentarily UNAVAILABLE, never a statement about doctor.
_is_transient_exec_error() {
  case "$1" in
    *"is restarting"* | *"is not running"* | *"is paused"* | *"No such container"* | *"not running"*) return 0 ;;
  esac
  return 1
}

# Run the doctor probe, retrying a bounded number of times while the only failure is
# a transient target unavailability. Returns 0 when a run COMPLETED (DOCTOR_RAW set,
# possibly empty — an empty completed run is RED), 125 when no exec service could be
# reached for a non-transient reason, and 126 when the target stayed unavailable for
# every attempt (a transient the caller must NOT page on).
run_doctor() {
  local attempt=1 rc
  while :; do
    _doctor_attempt && rc=0 || rc=$?
    if [ "$rc" -eq 0 ] && { [ -n "$DOCTOR_RAW" ] || ! _is_transient_exec_error "$DOCTOR_ERR"; }; then
      return 0
    fi
    if [ "$rc" -ne 0 ] && ! _is_transient_exec_error "$DOCTOR_ERR"; then
      return "$rc"
    fi
    if [ "$attempt" -ge "$DOCTOR_RETRIES" ]; then
      return 126
    fi
    log "doctor probe target unavailable (attempt $attempt/$DOCTOR_RETRIES) — retrying in ${DOCTOR_RETRY_DELAY}s"
    attempt=$((attempt + 1))
    sleep "$DOCTOR_RETRY_DELAY"
  done
}

# Trim ONLY well-known stale scratch (pytest / uv / claude) older than the age
# threshold from each service's disk temp roots, so a leaked temp dir can never
# fill the disk and wedge the box. Bounded (maxdepth 1, name-scoped, age-gated) and
# idempotent — a clean temp dir is a no-op. NEVER fatal: a trim failure (a service
# down, a read-only root) must not retire the only supervisor, so every branch is
# swallowed. A whole-tree dir like ``pytest-of-<user>`` is only "old" once no run
# has touched it for the threshold, so an ACTIVE test run is never trimmed.
trim_stale_temp() {
  local svc root
  for svc in $TEMP_TRIM_SERVICES; do
    for root in $TEMP_TRIM_ROOTS; do
      compose exec -T "$svc" bash -lc \
        "find '$root' -mindepth 1 -maxdepth 1 \\( -name 'pytest-*' -o -name 'uv-*' -o -name 'claude-*' \\) -mmin +$TEMP_TRIM_MIN_AGE_MIN -exec rm -rf {} + 2>/dev/null" \
        >/dev/null 2>&1 || true
    done
  done
  log "stale-temp trim swept ${TEMP_TRIM_SERVICES// /, } (>${TEMP_TRIM_MIN_AGE_MIN}min under ${TEMP_TRIM_ROOTS// /, })"
}

# The finding's deploy-sensitive class token, non-zero when it is not one. Keyed on
# the class rather than the message text: the clone-behind count changes between
# passes, so a text-keyed ledger could never match two observations of it.
#
# The patterns are deliberately narrow. A loose `holds the flock` would also swallow
# the INVERSE finding — "the worker holds the flock but these loops are not
# advancing" — a wedged worker, which is a real outage that must page on sight. A
# wording drift here un-gates a finding (back to a false page), never gates a real
# outage: the safe direction.
_deploy_sensitive_token() {
  case "$1" in
    *"no loop worker holds the flock"*) printf 'worker-flock-not-held' ;;
    *"slack-listener receiver is DOWN"*) printf 'slack-listener-down' ;;
    *"commit(s) behind origin/"*) printf 'clone-behind-origin' ;;
    *) return 1 ;;
  esac
}

# True when deploy.sh's single-convergence flock is held. READ-ONLY by construction:
# it matches the lock file's device+inode against /proc/locks and never opens the
# file for locking. A probe that briefly ACQUIRED the lock would make a deploy
# starting in that instant see it as busy — and deploy.sh exits 0 on a busy lock, so
# that deploy would be silently skipped. Non-zero means "not held, or cannot tell"
# (lock invisible from this container, /proc/locks unreadable, macOS host); the
# caller then falls back to the recreation signal, so a probe that cannot run fails
# toward alerting rather than toward silence.
deploy_lock_held() {
  local fields maj min ino
  [ -r /proc/locks ] || return 1
  fields="$(stat -c '%Hd %Ld %i' "$DEPLOY_LOCK" 2>/dev/null)" || return 1
  read -r maj min ino <<<"$fields"
  [ -n "${ino:-}" ] || return 1
  grep -qF "$(printf ' %02x:%02x:%s ' "$maj" "$min" "$ino")" /proc/locks
}

# True when a stack container was CREATED within the grace window — the fingerprint
# of the image swap. Created (never started) is the discriminating field: a
# crash-looping container restarts without being recreated, so a genuine outage is
# never mistaken for a deploy.
#
# The timestamp comes from `inspect .Created` (RFC3339 UTC, e.g.
# 2026-07-25T09:01:41.683288764Z), NEVER from `ps --format {{.CreatedAt}}`, whose
# local-zone abbreviation form ("… +0200 CEST") GNU date REFUSES to parse on a
# tzdata-less image — which this one is, so every sample would silently fail to
# parse and the probe would never fire on the box.
stack_recently_recreated() {
  local now created epoch
  local -a ids
  now="$(date -u +%s 2>/dev/null)" || return 1
  mapfile -t ids < <(docker ps --all --filter "label=com.docker.compose.project=$PROJECT" --format '{{.ID}}' 2>/dev/null)
  [ "${#ids[@]}" -gt 0 ] || return 1
  while IFS= read -r created; do
    [ -n "$created" ] || continue
    if ! epoch="$(date -u -d "$created" +%s 2>/dev/null)"; then
      log "unreadable container creation time ('$created') — not treating the stack as mid-deploy"
      continue
    fi
    if [ "$((now - epoch))" -lt "$DEPLOY_RECREATE_WINDOW" ]; then
      return 0
    fi
  done < <(docker inspect --format '{{.Created}}' "${ids[@]}" 2>/dev/null)
  return 1
}

deploy_in_flight() {
  if deploy_lock_held; then
    log "deploy lock $DEPLOY_LOCK is held — a convergence is in flight"
    return 0
  fi
  if stack_recently_recreated; then
    log "a stack container was created <${DEPLOY_RECREATE_WINDOW}s ago — the image swap is still settling"
    return 0
  fi
  return 1
}

# The re-surface ledger, "<episode> <digest>". Absent/unreadable reads as episode 0 with
# no digest — i.e. "the last pass was green", so a red pass still pages.
_read_red_state() { cat "$RED_STATE" 2>/dev/null || printf '0'; }

# Best-effort, exactly like the deploy-sensitive ledger: losing it costs one extra DM,
# never the supervisor.
_write_red_state() {
  printf '%s %s' "$1" "$2" >"$RED_STATE" 2>/dev/null ||
    log "could not persist the re-surface ledger at $RED_STATE"
}

# Record that the box is red with finding digest $1, and echo the current episode.
_observe_red() {
  local episode digest
  read -r episode digest <<<"$(_read_red_state)"
  _write_red_state "${episode:-0}" "$1"
  printf '%s' "${episode:-0}"
}

# Announce ONCE that a previously-reported finding set has cleared, and open the next
# episode. Silent when the last pass was already green (nothing to un-say). The episode
# bump is what lets the SAME finding set page again if it returns: without it the
# returning set would reuse its own pre-clear key and the notify seam would swallow it.
_announce_findings_cleared() {
  local episode digest
  read -r episode digest <<<"$(_read_red_state)"
  [ -n "${digest:-}" ] || return 0
  printf 'teatree watchdog: the red findings previously reported (%s) have CLEARED — the box is green again.' "$digest" \
    | notify_owner "watchdog:cleared:$digest:$episode"
  _write_red_state "$((${episode:-0} + 1))" ""
}

_read_pending_findings() { cat "$DEPLOY_PENDING_STATE" 2>/dev/null || true; }

# Best-effort: an unwritable state root must never retire the only supervisor.
# Losing the ledger costs one extra pass before a persisting finding pages.
_write_pending_findings() {
  printf '%s' "$1" >"$DEPLOY_PENDING_STATE" 2>/dev/null ||
    log "could not persist the deploy-sensitive ledger at $DEPLOY_PENDING_STATE"
}

# Emit (stdout) the FAIL messages that page THIS pass, reading all of them on stdin
# one per line. A deploy-sensitive finding is dropped while a convergence is in
# flight, and otherwise pages only on a SECOND consecutive observation — so a swap
# window that ended moments ago cannot page either, while a worker down over two
# clean passes pages exactly as it did before. Log lines go to stderr, never into
# the DM body.
_findings_that_page() {
  local in_flight=false previous observed="" message token
  deploy_in_flight && in_flight=true
  previous="$(_read_pending_findings)"
  while IFS= read -r message; do
    [ -n "$message" ] || continue
    if ! token="$(_deploy_sensitive_token "$message")"; then
      printf '%s\n' "$message"
      continue
    fi
    if [ "$in_flight" = true ]; then
      log "deploy in flight — skipping $token this pass"
      continue
    fi
    observed="$observed$token"$'\n'
    case "$previous" in
      *"$token"*) printf '%s\n' "$message" ;;
      *) log "first observation of $token with no deploy in flight — re-probing next pass before paging" ;;
    esac
  done
  _write_pending_findings "$observed"
}

# The three hard-outage alarms below key on a DAILY bucket (`%Y%m%d`), not an
# hourly one: `notify_user` dedups on the key, so an hourly bucket re-DM'd the
# identical "stack down" alarm every hour (13+ overnight copies observed). A
# daily bucket collapses a persisting unchanged outage to at most one DM/day
# while still re-alerting each day it persists and on a next-day recurrence.
run_pass() {
  # Sample who is down BEFORE the restart — afterwards the evidence is gone, which is
  # why an outage the watchdog silently healed left no trace anyone could act on.
  local now down still_down
  now="$(date -u +%s 2>/dev/null || printf 0)"
  down="$(down_app_services)"

  log "restarting any down services (gated on init state)"
  if ! restart_down_services; then
    # The stack could not even be brought up — the strongest outage signal.
    printf 'teatree watchdog: `docker compose up -d` FAILED on the box — the stack is DOWN and could not be restarted. SSH in and inspect `docker compose -p %s logs`.' "$PROJECT" \
      | notify_owner "watchdog:compose-up-failed:$(date -u +%Y%m%d)"
    return 0
  fi

  # Announce BEFORE stamping: the durations come from the pre-restart ledger, and
  # stamping first would report a just-recovered service as down ~0 min.
  still_down="$(down_app_services)"
  announce_repaired_services "$down" "$still_down" "$now"
  _record_liveness "$now" "$still_down"

  # Guard against a temp-scratch leak filling the disk (never fatal — see fn).
  trim_stale_temp

  local doctor_rc
  run_doctor && doctor_rc=0 || doctor_rc=$?
  if [ "$doctor_rc" -eq 126 ]; then
    # The probe could not RUN: its target was restarting / not running for every
    # attempt. That is a transient (#3651) — a retry is the whole remedy, and it
    # must never page the owner the way a completed-but-silent doctor does.
    log "doctor probe target unavailable after $DOCTOR_RETRIES attempts — transient, not paging"
    return 0
  fi
  if [ "$doctor_rc" -ne 0 ]; then
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
    printf 'teatree watchdog: `t3 doctor check --json` ran but produced NO parseable verdict — doctor may be crashing on the box. SSH in and run `t3 doctor check` in `docker compose -p %s exec teatree-worker`.' "$PROJECT" \
      | notify_owner "watchdog:doctor-no-verdict:$(date -u +%Y%m%d)"
    return 0
  fi

  case "$json" in
    *'"ok": true'*)
      log "doctor: all green"
      # "Two CONSECUTIVE passes": a green pass resets the two-strikes ledger.
      _write_pending_findings ""
      _announce_findings_cleared
      return 0
      ;;
  esac

  # Red: build the DM body from the FAIL messages and the idempotency key from their
  # volatility-normalized IDENTITIES, which is what makes "same findings as last pass"
  # cheap and exact. Keying on the rendered body instead re-paged an unchanged condition
  # on every pass, because several FAIL lines carry a counter that ticks between passes.
  local fails body digest key
  fails="$(printf '%s' "$json" | _fail_messages)"
  if [ -n "$fails" ]; then
    fails="$(printf '%s\n' "$fails" | _findings_that_page)"
    if [ -z "$fails" ]; then
      log "every red finding was deploy-sensitive and gated this pass — not paging"
      return 0
    fi
    body="$(printf '%s\n' "$fails" | cut -f2- | sed 's/^/- /')"
    digest="$(printf '%s\n' "$fails" | cut -f1 | sort -u | _stable_key)"
  else
    # Nothing to classify — a red verdict is never silently dropped.
    body="$(_generic_fail_body)"
    digest="$(printf '%s' "$body" | _stable_key)"
  fi
  key="watchdog:red:$digest:$(_observe_red "$digest"):$(day_bucket)"
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

# Extract the FAIL findings from the doctor JSON (read on stdin) as `<identity>\t<message>`
# lines, so each can be classified for deploy-sensitivity (on the message) and digested
# for the DM key (on the identity). `identity` is the doctor's volatility-normalized form
# — see teatree.cli.doctor.finding_digest — and falls back to the message itself, so a
# rolling deploy where the worker still runs an older doctor keeps paging normally rather
# than going silent. Empty when there are no FAILs or python3 is absent; the caller then
# falls back to `_generic_fail_body`. Uses `python3 -c` rather than a `-` heredoc: a
# `python3 - <<'PY'` feeds the heredoc as the PROGRAM on stdin, leaving `sys.stdin` at EOF
# so the piped verdict is never read — every body would be the generic fallback.
_fail_messages() {
  command -v python3 >/dev/null 2>&1 || return 0
  python3 -c '
import json, sys
def flat(text):
    return " ".join(str(text).split())
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for f in data.get("findings", []):
    if f.get("level") == "FAIL":
        message = flat(f.get("message", ""))
        print(flat(f.get("identity") or message) + "\t" + message)
'
}

# The body for a red verdict whose FAIL lines could not be extracted.
_generic_fail_body() {
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' "- (see \`t3 doctor check\` on the box for detail)"
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
