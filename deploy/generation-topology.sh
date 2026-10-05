#!/usr/bin/env bash
# Shared image-baked Compose files and host path identity for generation launches.

generation_compose_project() {
    printf '%s\n' "${TEATREE_COMPOSE_PROJECT:-${1:-teatree}}"
}

generation_container_home() {
    sed -n 's|^[[:space:]]*-[[:space:]]*teatree_clones:\(.*\)/workspace[[:space:]]*$|\1|p' "$1" | head -n1
}

generation_needs_host_identity() {
    local container_home
    container_home="$(generation_container_home "$1")"
    [ -n "$container_home" ] && [ "${TEATREE_HOST_HOME:-$container_home}" != "$container_home" ]
}

generation_compose_files() {
    local image="$1" scratch="$2" name
    for name in docker-compose.yml docker-compose.generation.yml docker-compose.host-identity.yml; do
        [ -s "$scratch/$name" ] && continue
        if docker run --rm --pull never --entrypoint sh "$image" -c "cat \"\$TEATREE_CLONE_DIR/deploy/$name\"" >"$scratch/$name.$$"; then
            mv -f "$scratch/$name.$$" "$scratch/$name"
        else
            rm -f "$scratch/$name.$$"
            echo "generation-topology: cannot read deploy/$name from $image, so its compose topology is unknown — is that image built on this host?" >&2
            return 1
        fi
    done
    GENERATION_COMPOSE_ARGS=(-f "$scratch/docker-compose.yml" -f "$scratch/docker-compose.generation.yml")
    generation_needs_host_identity "$scratch/docker-compose.yml" && GENERATION_COMPOSE_ARGS+=(-f "$scratch/docker-compose.host-identity.yml")
    return 0
}
