# The GPG home a `docker exec` can actually use.
#
# deploy/entrypoint.sh's resolve_gnupg_home redirects GNUPGHOME to a container-local
# copy of the key material whenever the host's GPG mount cannot host the S.* sockets
# gpg-agent and keyboxd bind. That export reaches only the role's own process tree;
# an exec starts from the container environment fixed at create time, so it keeps the
# baked home and every `pass show` returns EMPTY with rc=0 — a credential failure
# shaped exactly like the feature being switched off.
#
# A compose `environment:` pin cannot carry the value: it differs per host, and
# pinning the derived path would make the entrypoint's own `[ -d "$GNUPGHOME" ]`
# guard skip the copy that creates it. So this reads the EVIDENCE the entrypoint left
# on disk rather than re-deriving anything — key material in the derived home exists
# only where the entrypoint decided the mount could not host the sockets. On the box
# the tmpfs stays empty and GNUPGHOME is left exactly as the image baked it.
#
# deploy/t3 carries the same predicate inline for the non-login `sh -c` it runs;
# tests/test_deploy_t3_container_gnupg.py pins the two in sync.
_teatree_gnupg_derived="${TEATREE_GNUPG_RUNTIME_DIR:-/home/teatree/.gnupg-run}/gnupg"
for _teatree_gnupg_key in "$_teatree_gnupg_derived"/private-keys-v1.d/*.key; do
    if [ -f "$_teatree_gnupg_key" ]; then
        GNUPGHOME="$_teatree_gnupg_derived"
        export GNUPGHOME
        break
    fi
done
unset _teatree_gnupg_derived _teatree_gnupg_key
