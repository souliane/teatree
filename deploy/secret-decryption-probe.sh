#!/bin/sh
# Does secret decryption work inside the running containers, RIGHT NOW?
#
# Answers that in about two seconds, on the host, with no `t3` and no Django boot.
# `deploy/README.md` § "Is decryption working right now?" carries the operator view; what
# follows is only the why behind the shape, which is not guessable from the code.
#
# EVERY ANSWER CARRIES A CLOCK AND AN INSTANCE. A verdict with neither cannot be checked
# against anything, so it gets relayed: a fixed GPG fault was reported as current for the
# rest of the day because each session repeated the last one's finding, and one session's
# healthy worker was read as a healthy admin while the admin was the broken one. Hence a
# measurement stamp, each container's own creation time (a container newer than a quoted
# result voids it), and a separate verdict per container — never one aggregate "GPG: OK".
#
# THE THREE INVOCATION SHAPES ARE NOT INTERCHANGEABLE. gpg-agent and keyboxd bind their
# sockets inside GNUPGHOME, which a bind mount served over virtiofs cannot host; a
# `docker exec` shell that does not inherit GNUPGHOME therefore lands on the host keybox
# and gets its own agent. Login, non-login and bare exec resolve that variable
# differently, so one shape passing says nothing about the other two.
#
# GNUPGHOME IS ITSELF A VERDICT. Shapes disagreeing on it, or any shape resolving the
# host GPG dir (a read-only SOURCE, never GNUPGHOME), is the fault topology and is RED
# even while the decrypt still succeeds.
#
# LENGTH, NOT JUST rc. A store answering rc=0 with EMPTY output is the quiet failure —
# the one that no-ops with no error — so success needs a non-zero length wherever a
# length can be taken.
#
# NEVER PRINTS A SECRET. Output gets pasted into reports. The length is computed INSIDE
# the container so plaintext never crosses the docker boundary; the bare shape, where a
# pipe would need the very shell that shape exists to avoid, is proved by exit status
# with stdout discarded.
#
# FAILS LOUD. An unreachable daemon, an empty container set, a missing container and a
# check that does not answer are all RED: "I could not look" is not "it is fine". The one
# exclusion is a container with no credential plane (the watchdog runs as root with no
# store, delegating credential work to its siblings) — decided by a create-time fact
# confirmed at runtime, never by service name, printed on its own line with its reason,
# and RED rather than green if it turns out to be every container.
#
# Usage:
#   deploy/secret-decryption-probe.sh                 # every container in the project
#   deploy/secret-decryption-probe.sh NAME [NAME...]  # only these
#
# Env: TEATREE_COMPOSE_PROJECT (default teatree)
#      TEATREE_GITLAB_TOKEN_PASS_PATH — explicit bootstrap entry to probe; required
#      TEATREE_SECRET_READ_DEADLINE_SECONDS (default 10)
#
# Exit codes: 0 = GREEN (every container with a credential plane decrypted in every
# shape), 1 = RED.

set -u

project="${TEATREE_COMPOSE_PROJECT:-teatree}"
pass_path="${TEATREE_GITLAB_TOKEN_PASS_PATH:-}"
deadline="${TEATREE_SECRET_READ_DEADLINE_SECONDS:-10}"

if [ -z "$pass_path" ]; then
    echo "secret-decryption probe: TEATREE_GITLAB_TOKEN_PASS_PATH must name the explicit bootstrap entry to probe." >&2
    exit 1
fi

# The deadline reaches `timeout` inside the container, which accepts a fraction, but it
# also feeds the host's integer-only arithmetic below. Refuse a value that is neither
# rather than letting the shell abort mid-sweep with a raw arithmetic error.
case "$deadline" in
'' | *[!0-9.]* | *.*.*) echo "secret-decryption probe: TEATREE_SECRET_READ_DEADLINE_SECONDS must be a number, got '$deadline'." >&2 && exit 1 ;;
esac
deadline_whole="${deadline%%.*}"
case "$deadline_whole" in '' | *[!0-9]*) deadline_whole=0 ;; esac

stamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
started_epoch="$(date -u +%s)"
host="$(hostname 2>/dev/null || echo unknown)"

tmp="$(mktemp -d "${TMPDIR:-/tmp}/t3-secret-probe.XXXXXX")" || {
    echo "secret-decryption probe: cannot create a temp dir; refusing to report a result." >&2
    exit 1
}
trap 'rm -rf "$tmp"' EXIT INT TERM

red=0
failures=""
note() { printf '%s\n' "$1"; }

# The program the two SHELL shapes run inside the container. Single-quoted at the call
# site so the host shell never expands it. It prints four fields and nothing else: the
# GNUPGHOME this shape resolved, the host GPG dir it was told to treat as a read-only
# SOURCE, `pass`'s own exit code, and the first line's byte length. `$out` holds the
# plaintext only for as long as the length takes to compute, and never leaves the
# container.
#
# The rc is captured from `pass` DIRECTLY rather than through a pipe: POSIX sh has no
# PIPESTATUS, and a pipeline would report `wc`'s success as the store's.
in_container_program='
out=$(timeout "$2" pass show "$1" 2>/dev/null); rc=$?
printf "%s|%s|%s|%s\n" "${GNUPGHOME:-<unset>}" "${TEATREE_HOST_GNUPG_DIR:-<unset>}" "$rc" \
    "$(printf %s "$out" | head -n1 | tr -d "\n" | wc -c | tr -d " ")"
'

# rc semantics are the ones deploy/t3's credential prologue already documents, so this
# probe and the code path it certifies agree on what a failure looks like.
explain_rc() {
    case "$1" in
    124) echo "no answer within ${deadline}s — a WEDGED store, not a missing secret" ;;
    127) echo "\`pass\` absent from the container" ;;
    1) echo "secret not found, or decryption refused" ;;
    0) echo "rc=0 but the store answered EMPTILY — the SILENT failure, not a success" ;;
    *) echo "gpg error (rc=$1)" ;;
    esac
}

check_shell_shape() {
    # $1 container, $2 sh flag (-lc | -c)
    docker exec "$1" sh "$2" "$in_container_program" _ "$pass_path" "$deadline" 2>/dev/null
}

check_bare_shape() {
    # No shell at all: `pass` is exec'd directly, so the process starts from the
    # container's CREATE-TIME environment — the venue the fault topology breaks first.
    # Proved by exit status with stdout discarded; see the header on why not by length.
    home="$(docker exec "$1" printenv GNUPGHOME 2>/dev/null)" || home="<unset>"
    docker exec "$1" timeout "$deadline" pass show "$pass_path" >/dev/null 2>&1
    printf '%s|?|%s|-\n' "${home:-<unset>}" "$?"
}

run_one() {
    case "$2" in
    login) check_shell_shape "$1" -lc ;;
    nonlogin) check_shell_shape "$1" -c ;;
    bare) check_bare_shape "$1" ;;
    esac
}

# ONE inspect for the whole set: name, creation time and mount destinations per line.
# Two serial docker calls per container is most of a small sweep's runtime, and a probe
# is only run if it is cheap.
inspect_all() {
    docker inspect \
        --format '{{.Name}}|{{.Created}}|{{range .Mounts}}{{.Destination}};{{end}}' \
        $probe_targets 2>/dev/null >"$tmp/inspect"
}
inspect_line() { grep "^/$1|" "$tmp/inspect" 2>/dev/null | head -n1; }

container_created() {
    line="$(inspect_line "$1")"
    [ -n "$line" ] || return 1
    line="${line#*|}"
    printf '%s\n' "$(printf %s "${line%%|*}" | cut -c1-19)Z"
}

# Two signals, in that order, so neither alone can wrongly exclude a container. The
# declared mount is the create-time intent; the runtime check exists because a store
# baked into an image would declare no mount either, and excluding on the mount alone
# would then hide a real store that cannot decrypt.
has_credential_plane() {
    line="$(inspect_line "$1")"
    case "${line##*|}" in *.password-store\;*) return 0 ;; esac
    docker exec "$1" sh -c '[ -d "${PASSWORD_STORE_DIR:-$HOME/.password-store}" ]' 2>/dev/null
}

# ---- enumerate -------------------------------------------------------------------

if ! docker info >/dev/null 2>&1; then
    note "secret-decryption probe — $stamp (host: $host)"
    note ""
    note "RED: the docker daemon did not answer. NOTHING was measured."
    note "     This is a RED result, not the absence of one: no claim about container"
    note "     secret decryption can be made from here."
    exit 1
fi

if [ "$#" -gt 0 ]; then
    containers="$*"
    source_label="named on the command line"
else
    containers="$(docker ps --filter "label=com.docker.compose.project=$project" \
        --filter status=running --format '{{.Names}}' 2>/dev/null | sort)"
    source_label="compose project '$project'"
fi

if [ -z "$containers" ]; then
    note "secret-decryption probe — $stamp (host: $host)"
    note ""
    note "RED: no running containers found ($source_label)."
    note "     An empty set is not a pass — nothing here could have answered, so the"
    note "     honest verdict is that decryption is UNPROVEN."
    exit 1
fi

# ---- measure, in parallel --------------------------------------------------------
#
# One `docker exec` costs ~0.75s of pure daemon overhead while the gpg decrypt inside it
# costs ~0. Sequentially that overhead IS the runtime; fanned out it is one exec's worth,
# which is what keeps a full sweep near a second and therefore keeps it run.

# Partition first. A container that does not exist is RED; one with no credential plane
# is excluded WITH ITS REASON; only the rest are worth three execs.
probe_targets="$containers"
inspect_all

probed=""
excluded=""
missing=""
for c in $containers; do
    if [ -z "$(inspect_line "$c")" ]; then
        missing="$missing $c"
    elif has_credential_plane "$c"; then
        probed="$probed $c"
    else
        excluded="$excluded $c"
    fi
done

job_ids=""
for c in $probed; do
    for shape in login nonlogin bare; do
        job_id="$c.$shape"
        (run_one "$c" "$shape" >"$tmp/$job_id" 2>/dev/null; : >"$tmp/$job_id.done") &
        printf '%s\n' "$!" >"$tmp/$job_id.pid"
        job_ids="$job_ids $job_id"
    done
done

# Bounded wait. `timeout` bounds the read INSIDE the container, but a wedged daemon can
# hang `docker exec` itself, and a probe that hangs is a probe nobody runs. Polling on
# `kill -0` is the portable ceiling for that: macOS and Linux alike, needing no host
# `timeout` (which macOS does not ship) and no uname branch.
host_deadline=$((deadline_whole + 15))
ticks=0
max_ticks=$((host_deadline * 5))
while [ "$ticks" -lt "$max_ticks" ]; do
    alive=0
    for job_id in $job_ids; do
        [ -e "$tmp/$job_id.done" ] || alive=1
    done
    [ "$alive" -eq 0 ] && break
    sleep 0.2
    ticks=$((ticks + 1))
done
for job_id in $job_ids; do
    if [ ! -e "$tmp/$job_id.done" ]; then
        kill -9 "$(cat "$tmp/$job_id.pid")" 2>/dev/null
    fi
done
for job_id in $job_ids; do
    wait "$(cat "$tmp/$job_id.pid")" 2>/dev/null
done

finished="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
elapsed=$(($(date -u +%s) - started_epoch))

# ---- report ----------------------------------------------------------------------

note "secret-decryption probe — measured $stamp .. $finished (${elapsed}s, host: $host)"
note "  scope: $(echo "$probed" | wc -w | tr -d ' ') of $(echo "$containers" | wc -w | tr -d ' ') container(s) from $source_label carry a credential plane"
note "  proof: \`pass show $pass_path\` per container per invocation shape, reported by"
note "         byte length / exit status only — never by value."
note ""
printf '%-32s %-21s %-13s %-13s %-9s %s\n' \
    CONTAINER CREATED LOGIN NON-LOGIN BARE GNUPGHOME

for c in $missing; do
    red=1
    printf '%-32s %-21s %-13s %-13s %-9s %s\n' \
        "$c" "<no such container>" RED RED RED "-"
    failures="$failures
  $c: no such container. Named but not present — RED, because a probe that cannot find
    its target has proved nothing about it."
done

for c in $excluded; do
    printf '%-32s %-21s %-13s %-13s %-9s %s\n' \
        "$c" "$(container_created "$c")" "n/a" "n/a" "n/a" "no credential plane (no pass-store)"
done

for c in $probed; do
    # Instance identity. A container newer than a quoted result voids that quote, so the
    # creation time is reported even when — especially when — the verdict is green.
    created="$(container_created "$c")" || created="<no such container>"

    homes=""
    hostdir=""
    cells=""
    for shape in login nonlogin bare; do
        line="$(tail -n1 "$tmp/$c.$shape" 2>/dev/null)"
        home="${line%%|*}"
        r1="${line#*|}"
        hdir="${r1%%|*}"
        r2="${r1#*|}"
        rc="${r2%%|*}"
        len="${r2##*|}"
        [ "$hdir" = "?" ] || [ -z "$hdir" ] || hostdir="$hdir"

        if [ -z "$line" ]; then
            cell="NO-ANSWER"
            red=1
            failures="$failures
  $c/$shape: no answer within ${host_deadline}s — the container could not be reached,
    or the exec never returned. Unreachable is RED, not green."
        elif [ "$rc" = 0 ] && { [ "$len" = "-" ] || [ "${len:-0}" -gt 0 ] 2>/dev/null; }; then
            [ "$len" = "-" ] && cell="OK rc=0" || cell="OK len=$len"
            homes="$homes $home"
        else
            cell="FAIL rc=$rc"
            red=1
            failures="$failures
  $c/$shape: $(explain_rc "$rc")  [GNUPGHOME=$home]"
            homes="$homes $home"
        fi
        cells="$cells|$cell"
    done

    c1="${cells#|}"
    r1="${c1#*|}"
    c1="${c1%%|*}"
    c2="${r1%%|*}"
    c3="${r1#*|}"

    uniq_homes="$(printf '%s\n' $homes | sort -u)"
    home_display="$(printf '%s' "$uniq_homes" | tr '\n' ' ')"

    if [ "$(printf '%s\n' "$uniq_homes" | wc -l | tr -d ' ')" -gt 1 ]; then
        red=1
        home_display="DIVERGENT: $home_display"
        failures="$failures
  $c: the invocation shapes resolved DIFFERENT GNUPGHOME values ($home_display).
    That is the historical fault signature — one shape landing on the bind-mounted host
    home instead of the container-local one — reported even though the decrypt above
    may still have succeeded."
    fi

    if [ -n "$hostdir" ] && [ "$hostdir" != "<unset>" ] &&
        printf '%s\n' $homes | grep -qxF "$hostdir"; then
        red=1
        home_display="HOST-HOME: $home_display"
        failures="$failures
  $c: a shape resolved GNUPGHOME to the HOST GPG dir ($hostdir), which is meant to be a
    read-only SOURCE and never GNUPGHOME. This is the pre-fix topology; recreating the
    stack re-applies the create-time pin with no image rebuild."
    fi

    printf '%-32s %-21s %-13s %-13s %-9s %s\n' \
        "$c" "$created" "$c1" "$c2" "$c3" "$home_display"
done

note ""
if [ -z "$probed" ]; then
    red=1
    failures="$failures
  Nothing was probed: no container in scope carries a credential plane. That proves
    nothing about secret decryption, so the verdict is RED rather than an empty pass."
fi

n_excluded="$(echo "$excluded" | wc -w | tr -d ' ')"
if [ "$red" -eq 0 ]; then
    note "VERDICT: GREEN — all $(echo "$probed" | wc -w | tr -d ' ') container(s) with a credential plane decrypted in"
    note "         every invocation shape; $n_excluded excluded above as having none."
else
    note "VERDICT: RED"
    printf '%s\n' "$failures"
fi
note ""
note "This describes $finished and nothing later, for the container instances created at"
note "the times above. Re-run it rather than quoting it; if a container is newer than a"
note "quoted result, that result is void. Repeating a stale reading as current is the"
note "failure this tool exists to prevent."

[ "$red" -eq 0 ] || exit 1
