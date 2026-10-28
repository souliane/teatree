#!/usr/bin/env bash

BUILD_NEED_KIB=$((17 * 1024 * 1024))
BUILD_FREE_KIB=""
BUILD_RESERVED_KIB=""
BUILD_SPACE_PATH=""

build_space_gib() {
    awk -v kib="$1" 'BEGIN { printf "%.1f GiB", kib / 1048576 }'
}

read_build_space() {
    local docker_root path stats total used available reserved
    BUILD_FREE_KIB=""
    BUILD_RESERVED_KIB=""
    BUILD_SPACE_PATH=""
    docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
    for path in / "${TMPDIR:-/tmp}" ${docker_root:+"$docker_root"}; do
        if ! stats="$(df -Pk "$path" 2>/dev/null | awk 'NR == 2 { print $2, $3, $4 }')"; then
            return 1
        fi
        read -r total used available <<<"$stats"
        case "$total:$used:$available" in *[!0-9:]* | :* | *::*) return 1 ;; esac
        reserved=$((total - used - available))
        if [ -z "$BUILD_FREE_KIB" ] || [ "$available" -lt "$BUILD_FREE_KIB" ]; then
            BUILD_FREE_KIB="$available"
            BUILD_RESERVED_KIB="$reserved"
            BUILD_SPACE_PATH="$path"
        fi
    done
    [ -n "$BUILD_FREE_KIB" ]
}

require_build_space() {
    local caller="$1"
    if ! read_build_space; then
        echo "$caller: NOT BUILDING - free and reserved disk space could not be measured." >&2
        return 1
    fi
    if [ "$BUILD_FREE_KIB" -lt "$BUILD_NEED_KIB" ]; then
        echo "$caller: NOT BUILDING - $(build_space_gib "$BUILD_FREE_KIB") free, $(build_space_gib "$BUILD_RESERVED_KIB") reserved at $BUILD_SPACE_PATH; need $(build_space_gib "$BUILD_NEED_KIB")." >&2
        return 1
    fi
    echo "$caller: $(build_space_gib "$BUILD_FREE_KIB") free, $(build_space_gib "$BUILD_RESERVED_KIB") reserved at $BUILD_SPACE_PATH; need $(build_space_gib "$BUILD_NEED_KIB")."
}
