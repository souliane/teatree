# The GPG-home resolver functions of deploy/entrypoint.sh at 4f7312cada759338356e2eddcce175b06a0b5378,
# vendored because the CI test image carries no git history to read them from.

path_fstype() {
    local target="$1" mounts="${TEATREE_PROC_MOUNTS:-/proc/mounts}" point type best_point="" best_type=""
    [ -r "$mounts" ] || return 0
    while read -r _ point type _; do
        case "$point" in
            /) ;;
            *)
                case "$target" in
                    "$point" | "$point"/*) ;;
                    *) continue ;;
                esac
                ;;
        esac
        if [ "${#point}" -ge "${#best_point}" ]; then
            best_point="$point"
            best_type="$type"
        fi
    done <"$mounts"
    printf '%s' "$best_type"
}

fstype_hosts_unix_sockets() {
    case "$1" in
        ext2 | ext3 | ext4 | xfs | btrfs | zfs | f2fs | jfs | reiserfs | overlay | overlayfs | tmpfs | ramfs) return 0 ;;
        *) return 1 ;;
    esac
}

derive_container_gnupg_home() {
    local source="$1" derived="$2" name
    rm -rf "$derived"
    mkdir -p "$derived" || return 1
    chmod 700 "$derived"
    for name in common.conf gpg.conf pubring.kbx pubring.gpg trustdb.gpg; do
        if [ -f "$source/$name" ]; then
            cp -p "$source/$name" "$derived/$name"
        fi
    done
    if [ -d "$source/private-keys-v1.d" ]; then
        mkdir -p "$derived/private-keys-v1.d"
        chmod 700 "$derived/private-keys-v1.d"
        find "$source/private-keys-v1.d" -maxdepth 1 -type f -name '*.key' \
            -exec cp -p {} "$derived/private-keys-v1.d/" \;
    fi
    if [ -f "$source/public-keys.d/pubring.db" ]; then
        mkdir -p "$derived/public-keys.d"
        chmod 700 "$derived/public-keys.d"
        cp -p "$source/public-keys.d/pubring.db" "$derived/public-keys.d/pubring.db"
    fi
    return 0
}

resolve_gnupg_home() {
    local fstype derived
    [ -n "${GNUPGHOME:-}" ] && [ -d "$GNUPGHOME" ] || return 0
    fstype="$(path_fstype "$GNUPGHOME")"
    fstype_hosts_unix_sockets "$fstype" && return 0
    derived="${TEATREE_GNUPG_RUNTIME_DIR:-/home/teatree/.gnupg-run}/gnupg"  # privacy-scan:allow container home
    if ! derive_container_gnupg_home "$GNUPGHOME" "$derived"; then
        echo "entrypoint: WARN GNUPGHOME $GNUPGHOME is on '$fstype' (cannot host the gpg-agent/keyboxd sockets) but a container-local copy at $derived could not be created - keeping $GNUPGHOME, gpg reads may fail" >&2
        return 0
    fi
    # Absence stays a no-op, not a new failure: a host with no key material
    # yields an empty derived home, gpg finds no keys exactly as it did before,
    # and init_preflight reports the SAME message it always did.
    echo "entrypoint: GNUPGHOME $GNUPGHOME is on '$fstype', which cannot host the gpg-agent/keyboxd sockets - using a container-local copy of the key material at $derived (the host GPG home is left untouched)"
    export GNUPGHOME="$derived"
}
