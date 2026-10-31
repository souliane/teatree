#!/usr/bin/env bash
# The in-progress record a convergence keeps in the lock file under the flock, shared by
# deploy.sh and roll.sh. The caller sets DEPLOY_LOCK.

# "<pid> <heartbeat> <deadline>". The first write truncates, so no longer record a killed
# holder left behind survives it.
write_deploy_record() {
    printf '%s %s %s\n' "$1" "$3" "$2" >"$DEPLOY_LOCK"
}

# A beat rewrites the record in place: `>` empties the file before printf refills it, and a
# reader caught in that gap sees no convergence at all. One holder's record never changes
# length (fixed pid and deadline, a 10-digit epoch), so nothing of the old one is left over.
# Never a rename over it either: the flock lives on this inode. An emptied record stays
# empty — the exit trap cleared it.
beat_deploy_record() {
    [ -s "$DEPLOY_LOCK" ] || return 0
    printf '%s %s %s\n' "$1" "$(date -u +%s)" "$2" 1<>"$DEPLOY_LOCK"
}
