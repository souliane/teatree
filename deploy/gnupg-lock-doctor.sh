#!/bin/sh
# Name the ONE cause behind three unrelated-looking host failures:
#
#     git commit  -> gpg: signing failed: Operation timed out
#     pass show   -> gpg: decryption failed: No secret key
#     t3 <write>  -> no GitLab token (its `pass` read is the one above)
#
# All three are gnupg blocking on `public-keys.d/pubring.db.lock`. The lock
# records `<pid>\n<node>\n`, and gnupg reclaims a stale one ONLY when <node> is
# the local node — across a pid namespace it cannot `kill(pid, 0)`, so it waits
# forever instead. A lock written inside a container therefore records the
# CONTAINER ID and is unreclaimable by the host BY DESIGN, whether or not the
# container still exists.
#
# Read-only and non-destructive: it never removes a lock. A pid absent from the
# host process table is NOT evidence the holder is dead — it is very likely
# ALIVE in a container, where clearing the lock would corrupt the keybox under a
# running keyboxd. The remediation it prints therefore starts by proving the
# holder is gone.
#
# Runs on the HOST with no docker and no `t3`, deliberately: the wedge routinely
# co-occurs with a dead Docker daemon, and a check reachable only through the
# container is unreachable exactly when it is needed.
#
# Exit codes: 0 = no cross-namespace lock, 1 = one found (reported on stderr).

set -u

gnupg_home="${GNUPGHOME:-$HOME/.gnupg}"
lock="$gnupg_home/public-keys.d/pubring.db.lock"

[ -f "$lock" ] || exit 0

lock_pid="$(sed -n 1p "$lock" 2>/dev/null | tr -dc '0-9')"
lock_node="$(sed -n 2p "$lock" 2>/dev/null | tr -d '\r')"
local_node="$(hostname 2>/dev/null || echo unknown)"

# Same node: an ordinary local holder. gnupg breaks it itself once the pid dies,
# so there is nothing to report and nothing to do.
[ -n "$lock_node" ] && [ "$lock_node" != "$local_node" ] || exit 0

cat >&2 <<EOF
gnupg: the host GPG keybox is locked by a process OUTSIDE this host's namespace.

  lock   $lock
  pid    ${lock_pid:-<unreadable>}   (recorded node: $lock_node, this host: $local_node)

Every host gpg operation now blocks on it: signed \`git commit\`, \`pass show\`,
and any \`t3\` GitLab write (which resolves its token through \`pass\`). Three
different-looking errors, one cause.

gnupg can only reclaim a stale lock whose recorded node is the LOCAL node, so a
lock written inside a container is never reclaimed here — it does not expire.

A pid absent from this host's process table is NOT proof the holder is dead: it
is most likely ALIVE inside a container, and clearing the lock would corrupt the
keybox under a running keyboxd. Prove the holder is gone FIRST:

  docker ps -q | xargs -r -I{} docker exec {} ps -p ${lock_pid:-PID} -o pid=,args=
  # no output from every container, AND \`docker info\` failing/no containers
  # running, is the proof. Only then:
  mv '$lock' '$lock.orphaned-\$(date +%s)'

The durable fix is that no container ever opens this keybox. \$GNUPGHOME is
pinned at a container-local path (/home/teatree/.gnupg-run/gnupg) by BOTH
deploy/Dockerfile and deploy/docker-compose.yml, and the host GPG directory is
named separately as \$TEATREE_HOST_GNUPG_DIR — a read-only SOURCE, never
\$GNUPGHOME — so the entrypoint, a hand-issued docker exec, the loop's
subprocesses and the \`t3\` wrapper all resolve the same container-local home.
A container created before that lands still opens the host home directly. The
compose pin is create-time environment, so recreating the stack repairs it with
NO image rebuild:

  docker compose -f deploy/docker-compose.yml up -d

\`t3 doctor check\` reports any running container still carrying the host home.
EOF
exit 1
