#!/usr/bin/env bash
# Build the immutable image <repository>:<sha> from `git archive <sha>` of this checkout, in two
# layers: a base holding the interpreter and locked dependencies, tagged base-<hash of its inputs>
# and reused by every commit whose inputs match, and a thin per-commit layer with the source.
# The context is the commit, never the working tree. An image already here, or pullable from a
# registry repository, is not rebuilt. TEATREE_IMAGE_REPOSITORY (default teatree-factory) names a
# registry repository to pull from; TEATREE_PUSH_IMAGES=1 pushes what this run built.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
CORE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
SHA="${1:-}"
REVISION_LABEL=org.opencontainers.image.revision
MIN_BUILD_FREE_KIB=$((10 * 1024 * 1024))
# Generation images and bases kept by build time, beyond the ones a container or the promoted tag holds.
KEPT_GENERATIONS=3

if ! [[ "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "build-generation: usage: build-generation.sh <40-hex commit sha> (got '$SHA')" >&2
    exit 64
fi

# The same layout rule as deploy.sh: core checked out at <checkout>/vendor/teatree under a fork pyproject.
PROJECT_ROOT="$CORE_ROOT"
CORE_SUBDIR=""
if [ "$(basename "$CORE_ROOT")" = teatree ] && [ "$(basename "$(dirname "$CORE_ROOT")")" = vendor ] &&
    [ -f "$(dirname "$(dirname "$CORE_ROOT")")/pyproject.toml" ]; then
    PROJECT_ROOT="$(dirname "$(dirname "$CORE_ROOT")")"
    CORE_SUBDIR=vendor/teatree
fi
REPOSITORY="${TEATREE_IMAGE_REPOSITORY:-teatree-factory}"
IMAGE="$REPOSITORY:$SHA"
DOCKERFILE="${CORE_SUBDIR:+$CORE_SUBDIR/}deploy/Dockerfile"
BUILD_UID="${TEATREE_UID:-$(id -u)}"

if ! git -C "$PROJECT_ROOT" cat-file -e "$SHA^{commit}" 2>/dev/null; then
    echo "build-generation: commit $SHA is not in this checkout ($PROJECT_ROOT) — fetch it first." >&2
    exit 1
fi

image_revision() {
    docker image inspect --format "{{ index .Config.Labels \"$REVISION_LABEL\" }}" "$IMAGE" 2>/dev/null || true
}

# Only a repository that names a registry host can be pulled from.
pulled() {
    case "$REPOSITORY" in */*) docker pull --quiet "$1" >/dev/null 2>&1 ;; *) return 1 ;; esac
}

if [ "$(image_revision)" = "$SHA" ] || { pulled "$IMAGE" && [ "$(image_revision)" = "$SHA" ]; }; then
    echo "build-generation: $IMAGE already built (revision label matches) — nothing to do."
    exit 0
fi

# The base's inputs: the Dockerfile, the prek pin reader, the lock and every workspace member's manifest.
members="$(git -C "$PROJECT_ROOT" show "$SHA:uv.lock" | sed -nE 's/^source = \{ (editable|virtual) = "(.*)" \}$/\2/p')"
candidates=("$DOCKERFILE" "${CORE_SUBDIR:+$CORE_SUBDIR/}deploy/locked-version.sh" uv.lock .python-version)
for member in $members; do candidates+=("${member#./}/pyproject.toml"); done
candidates=("${candidates[@]/#.\/}")
base_inputs="$(git -C "$PROJECT_ROOT" ls-tree -r "$SHA" -- "${candidates[@]}")"
BASE="$REPOSITORY:base-$(printf '%s\n%s\n%s\n' "$base_inputs" "$BUILD_UID" "$CORE_SUBDIR" | git hash-object --stdin | cut -c1-16)"

as_gib() { awk -v kib="$1" 'BEGIN { printf "%.1f GiB", kib / 1048576 }'; }

# The least free space across `/` and docker's root: the build cache has been seen on `/` with the root elsewhere.
# Kept in KiB exactly as `df -Pk` prints it: awk arithmetic on it renders as 2.1e+09 under mawk.
least_free_kib() {
    local root path avail least=""
    root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
    for path in / ${root:+"$root"}; do
        avail="$({ df -Pk "$path" 2>/dev/null || true; } | awk 'NR == 2 { print $4 }')"
        case "$avail" in '' | *[!0-9]*) continue ;; esac
        if [ -z "$least" ] || [ "$avail" -lt "$least" ]; then
            least="$avail"
        fi
    done
    printf '%s' "$least"
}

ensure_build_space() {
    local free
    free="$(least_free_kib)"
    if [ -z "$free" ]; then
        echo "build-generation: WARNING could not read free disk space; building without the pre-build disk check." >&2
        return 0
    fi
    [ "$free" -ge "$MIN_BUILD_FREE_KIB" ] && return 0
    echo "build-generation: $(as_gib "$free") free, under the $(as_gib "$MIN_BUILD_FREE_KIB") a build needs - pruning the build cache and dangling images ..."
    docker builder prune -f >/dev/null || true
    docker image prune -f >/dev/null || true
    free="$(least_free_kib)"
    if [ -n "$free" ] && [ "$free" -lt "$MIN_BUILD_FREE_KIB" ]; then
        echo "build-generation: NOT BUILDING - $(as_gib "$free") free after pruning, under the $(as_gib "$MIN_BUILD_FREE_KIB") a build needs. Free space by hand, then re-run. The running stack is untouched." >&2
        exit 1
    fi
}

ensure_build_space

built_base=""
if ! docker image inspect "$BASE" >/dev/null 2>&1 && ! pulled "$BASE"; then
    echo "build-generation: building $BASE — its dependency inputs changed ..."
    git -C "$PROJECT_ROOT" archive --format=tar "$SHA" -- $(printf '%s\n' "$base_inputs" | cut -f2) |
        docker build -f "$DOCKERFILE" --target generation-base \
            --build-arg "TEATREE_CORE_SUBDIR=$CORE_SUBDIR" --build-arg "TEATREE_UID=$BUILD_UID" -t "$BASE" -
    built_base="$BASE"
fi

echo "build-generation: building $IMAGE on $BASE from git archive $SHA ($PROJECT_ROOT) ..."
git -C "$PROJECT_ROOT" archive --format=tar "$SHA" |
    docker build -f "$DOCKERFILE" --target generation --build-arg "TEATREE_BASE_IMAGE=$BASE" \
        --build-arg "TEATREE_GENERATION=$SHA" --build-arg "TEATREE_CORE_SUBDIR=$CORE_SUBDIR" -t "$IMAGE" -

if [ "$(image_revision)" != "$SHA" ]; then
    echo "build-generation: FATAL $IMAGE was built but its $REVISION_LABEL label is '$(image_revision)', not $SHA." >&2
    exit 1
fi
echo "build-generation: $IMAGE built."

# Compared by image id: `docker rmi` of one tag of a multi-tag image untags it even while a container
# runs it, so a name check would let the serving generation's own tag go with its restore path.
held_image_ids() {
    local container
    { docker ps -aq 2>/dev/null || true; } | while read -r container; do
        docker inspect --format '{{.Image}}' "$container" 2>/dev/null || true
    done
    docker image inspect --format '{{.Id}}' "${TEATREE_PROMOTED_TAG:-teatree-headless:latest}" 2>/dev/null || true
}

prune_older() {
    local pattern="$1" keep="$2" held="$3" ref id
    { docker image ls --format '{{.Repository}}:{{.Tag}}' "$REPOSITORY" 2>/dev/null || true; } |
        { grep -E ":$pattern\$" || true; } | tail -n "+$((KEPT_GENERATIONS + 1))" | while read -r ref; do
        [ "$ref" != "$keep" ] || continue
        id="$(docker image inspect --format '{{.Id}}' "$ref" 2>/dev/null || true)"
        if [ -z "$id" ] || printf '%s\n' "$held" | grep -qxF "$id"; then
            continue
        fi
        if docker rmi "$ref" >/dev/null 2>&1; then
            echo "build-generation: removed $ref, older than the last $KEPT_GENERATIONS and held by no container or promoted tag."
        fi
    done
}
HELD_IMAGE_IDS="$(held_image_ids)"
prune_older '[0-9a-f]{40}' "$IMAGE" "$HELD_IMAGE_IDS"
prune_older 'base-[0-9a-f]{16}' "$BASE" "$HELD_IMAGE_IDS"

if [ "${TEATREE_PUSH_IMAGES:-0}" = 1 ]; then
    for pushed in $built_base "$IMAGE"; do docker push --quiet "$pushed"; done
fi
