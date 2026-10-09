# test-path: cross-cutting — drives deploy/build-generation.sh as a real bash program (no src mirror).
"""``deploy/build-generation.sh <sha>`` builds a reusable base and a thin per-commit layer from ``git archive``."""

import io
import os
import shutil
import stat
import subprocess
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "build-generation.sh"
SYSTEM_PATH = os.defpath.strip(os.pathsep)
GIB = 1024 * 1024 * 1024

BASH = shutil.which("bash", path=SYSTEM_PATH) or ""
GIT = shutil.which("git") or ""


_DOCKER = """#!/bin/bash
printf '%s\\n' "$*" >>"$FAKE_DOCKER_LOG"
slot() { printf '%s' "$1" | tr '/:' '__'; }
# An image id is shared by every tag FAKE_IMAGE_IDS maps to it; an unmapped tag is its own image.
image_id() {
    local id
    id="$(awk -v ref="$1" '$1 == ref { print $2; exit }' "${FAKE_IMAGE_IDS:-/dev/null}" 2>/dev/null)"
    printf '%s' "${id:-sha256:$(slot "$1")}"
}
tags_of() {
    local id="$1" tag
    awk -v id="$id" '$2 == id { print $1 }' "${FAKE_IMAGE_IDS:-/dev/null}" 2>/dev/null | while read -r tag; do
        [ ! -f "$FAKE_IMAGES/$(slot "$tag")" ] || printf '%s\\n' "$tag"
    done
}
ref="${@: -1}"
case "$1 $2" in
"image inspect")
    [ -f "$FAKE_IMAGES/$(slot "$ref")" ] || exit 1
    case "${4:-}" in
    *.Id*) printf '%s\\n' "$(image_id "$ref")" ;;
    *) [ "$3" != --format ] || cat "$FAKE_IMAGES/$(slot "$ref")" ;;
    esac
    exit 0
    ;;
"ps -aq") awk '{ print $1 }' "${FAKE_CONTAINERS:-/dev/null}" 2>/dev/null; exit 0 ;;
"inspect --format") awk -v c="$ref" '$1 == c { print $2 }' "${FAKE_CONTAINERS:-/dev/null}" 2>/dev/null; exit 0 ;;
"info --format") echo /nonexistent-docker-root; exit 0 ;;
"image ls")
    prefix="$(slot "$ref")_"
    ls -t "$FAKE_IMAGES" | while read -r name; do
        case "$name" in "$prefix"*) printf '%s:%s\\n' "$ref" "${name#"$prefix"}" ;; esac
    done
    exit 0
    ;;
esac
case "$1" in
rmi)
    # Docker: a tag shared with another tag is only untagged, even while a container runs the image.
    id="$(image_id "$ref")"
    if [ "$(tags_of "$id" | wc -l)" -le 1 ] && awk -v id="$id" '$2 == id { found = 1 } END { exit !found }' \
        "${FAKE_CONTAINERS:-/dev/null}" 2>/dev/null; then
        echo "conflict: unable to remove $ref, container is using its referenced image" >&2
        exit 1
    fi
    rm -f "$FAKE_IMAGES/$(slot "$ref")"
    ;;
pull)
    [ -f "$FAKE_REGISTRY/$(slot "$ref")" ] || exit 1
    cp "$FAKE_REGISTRY/$(slot "$ref")" "$FAKE_IMAGES/$(slot "$ref")"
    ;;
build)
    label=base prev=""
    for arg in "$@"; do
        case "$prev" in --target) target="$arg" ;; -t) tag="$arg" ;; esac
        case "$arg" in TEATREE_GENERATION=*) label="${arg#TEATREE_GENERATION=}" ;; esac
        prev="$arg"
    done
    cat >"$FAKE_DOCKER_CONTEXT.$target"
    printf '%s' "$label" >"$FAKE_IMAGES/$(slot "$tag")"
    ;;
esac
exit 0
"""

_DF = """#!/bin/bash
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "/dev/fake 999999999 1 $FAKE_DF_AVAIL_KB 1% $2"
"""


def _executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _git(repo: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    result = subprocess.run([GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env)
    return result.stdout.strip()


@dataclass(frozen=True)
class _Checkout:
    root: Path
    script: Path
    sha: str
    dockerfile: str


def _checkout(tmp_path: Path, *, vendored: bool) -> _Checkout:
    root = tmp_path / "repo"
    core = root / "vendor" / "teatree" if vendored else root
    (core / "deploy").mkdir(parents=True)
    shutil.copy2(SCRIPT, core / "deploy" / "build-generation.sh")
    (core / "deploy" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (core / "deploy" / "locked-version.sh").write_text("echo 1\n", encoding="utf-8")
    members = ['source = { editable = "." }']
    if vendored:
        (core / "pyproject.toml").write_text("[project]\nname = 'teatree'\n", encoding="utf-8")
        members.append('source = { editable = "vendor/teatree" }')
    (root / "uv.lock").write_text("version = 1\n" + "\n".join(members) + "\n", encoding="utf-8")
    (root / "payload.txt").write_text("committed\n", encoding="utf-8")
    _git(root.parent, "init", "-q", "-b", "main", str(root))
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "init")
    dockerfile = "vendor/teatree/deploy/Dockerfile" if vendored else "deploy/Dockerfile"
    return _Checkout(root, core / "deploy" / "build-generation.sh", _git(root, "rev-parse", "HEAD"), dockerfile)


@dataclass(frozen=True)
class _Run:
    result: subprocess.CompletedProcess[str]
    docker_calls: list[str]
    context: Path
    base_context: Path


@dataclass(frozen=True)
class _Host:
    free_bytes: int = 50 * GIB
    label: str = ""
    docker: str = _DOCKER
    env: tuple[tuple[str, str], ...] = ()


_DEFAULT_HOST = _Host()


def _run(tmp_path: Path, checkout: _Checkout, sha: str, host: _Host = _DEFAULT_HOST, cwd: Path | None = None) -> _Run:
    stubs = tmp_path / "stub-bin"
    stubs.mkdir(exist_ok=True)
    _executable(stubs / "docker", host.docker)
    _executable(stubs / "df", _DF)
    log, context = tmp_path / "docker.log", tmp_path / "context.tar"
    images = tmp_path / "images"
    images.mkdir(exist_ok=True)
    (tmp_path / "registry").mkdir(exist_ok=True)
    log.write_text("", encoding="utf-8")
    if host.label:
        (images / f"teatree-factory_{sha}").write_text(host.label, encoding="utf-8")
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_DOCKER_CONTEXT": str(context),
        "FAKE_IMAGES": str(images),
        "FAKE_REGISTRY": str(tmp_path / "registry"),
        "FAKE_DF_AVAIL_KB": str(host.free_bytes // 1024),
        **dict(host.env),
    }
    result = subprocess.run(
        [BASH, str(checkout.script), sha], capture_output=True, text=True, env=env, check=False, timeout=60, cwd=cwd
    )
    return _Run(
        result,
        log.read_text(encoding="utf-8").splitlines(),
        tmp_path / "context.tar.generation",
        tmp_path / "context.tar.generation-base",
    )


def _builds(run: _Run, target: str = "") -> list[str]:
    builds = [call for call in run.docker_calls if call.split(" ", 1)[0] == "build"]
    return [call for call in builds if f"--target {target} " in call] if target else builds


def _commit(checkout: _Checkout, path: str, body: str) -> str:
    (checkout.root / path).write_text(body, encoding="utf-8")
    _git(checkout.root, "add", "-A")
    _git(checkout.root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", path)
    return _git(checkout.root, "rev-parse", "HEAD")


def _archive_members(context: Path) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(context.read_bytes())) as archive:
        for member in archive.getmembers():
            if (extracted := archive.extractfile(member)) is not None:
                members[member.name] = extracted.read()
    return members


class TestRefusals:
    @pytest.mark.parametrize("sha", ["main", "abc123", "A" * 40, ""])
    def test_a_value_that_is_not_a_full_commit_sha_is_refused(self, tmp_path: Path, sha: str) -> None:
        run = _run(tmp_path, _checkout(tmp_path, vendored=True), sha)

        assert run.result.returncode == 64
        assert "40-hex" in run.result.stderr
        assert run.docker_calls == []

    def test_a_commit_the_checkout_does_not_hold_is_refused(self, tmp_path: Path) -> None:
        run = _run(tmp_path, _checkout(tmp_path, vendored=True), "0" * 40)

        assert run.result.returncode != 0
        assert "not in this checkout" in run.result.stderr
        assert not _builds(run)

    def test_below_the_disk_floor_after_pruning_nothing_is_built(self, tmp_path: Path) -> None:
        checkout = _checkout(tmp_path, vendored=True)

        run = _run(tmp_path, checkout, checkout.sha, _Host(free_bytes=2 * GIB))

        assert run.result.returncode == 1
        assert "NOT BUILDING" in run.result.stderr
        assert "builder prune -f" in run.docker_calls
        assert not _builds(run)


class TestIdempotency:
    def test_an_image_whose_revision_label_matches_is_not_rebuilt(self, tmp_path: Path) -> None:
        checkout = _checkout(tmp_path, vendored=True)

        run = _run(tmp_path, checkout, checkout.sha, _Host(label=checkout.sha))

        assert run.result.returncode == 0, run.result.stderr
        assert "already built" in run.result.stdout
        assert not _builds(run)

    def test_a_tag_whose_label_disagrees_is_rebuilt(self, tmp_path: Path) -> None:
        checkout = _checkout(tmp_path, vendored=True)

        run = _run(tmp_path, checkout, checkout.sha, _Host(label="f" * 40))

        assert run.result.returncode == 0, run.result.stderr
        assert _builds(run)


class TestTheBuildContextIsTheCommit:
    def test_a_dirty_working_tree_never_reaches_the_image(self, tmp_path: Path) -> None:
        checkout = _checkout(tmp_path, vendored=True)
        (checkout.root / "payload.txt").write_text("dirty\n", encoding="utf-8")
        (checkout.root / "untracked-secret.txt").write_text("token\n", encoding="utf-8")

        run = _run(tmp_path, checkout, checkout.sha)

        assert run.result.returncode == 0, run.result.stderr
        members = _archive_members(run.context)
        assert members["payload.txt"] == b"committed\n"
        assert "untracked-secret.txt" not in members

    def test_the_checkout_is_left_as_it_was(self, tmp_path: Path) -> None:
        checkout = _checkout(tmp_path, vendored=True)
        (checkout.root / "payload.txt").write_text("dirty\n", encoding="utf-8")
        status_before = _git(checkout.root, "status", "--porcelain")

        _run(tmp_path, checkout, checkout.sha)

        assert _git(checkout.root, "rev-parse", "HEAD") == checkout.sha
        assert _git(checkout.root, "status", "--porcelain") == status_before

    def test_run_from_inside_the_vendored_core_the_context_is_still_the_whole_fork(self, tmp_path: Path) -> None:
        checkout = _checkout(tmp_path, vendored=True)

        run = _run(tmp_path, checkout, checkout.sha, cwd=checkout.root / "vendor" / "teatree")

        assert run.result.returncode == 0, run.result.stderr
        members = _archive_members(run.context)
        assert {"payload.txt", "pyproject.toml", "vendor/teatree/deploy/Dockerfile"} <= set(members)

    @pytest.mark.parametrize(("vendored", "core_subdir"), [(True, "vendor/teatree"), (False, "")])
    def test_the_generation_target_is_built_under_its_sha_tag(
        self, tmp_path: Path, *, vendored: bool, core_subdir: str
    ) -> None:
        checkout = _checkout(tmp_path, vendored=vendored)

        run = _run(tmp_path, checkout, checkout.sha)

        (build,) = _builds(run, "generation")
        assert f"-f {checkout.dockerfile}" in build
        assert "--build-arg TEATREE_BASE_IMAGE=teatree-factory:base-" in build
        assert f"--build-arg TEATREE_GENERATION={checkout.sha}" in build
        assert f"--build-arg TEATREE_CORE_SUBDIR={core_subdir} " in build
        assert f"-t teatree-factory:{checkout.sha}" in build
        assert build.endswith(" -")

    def test_a_build_that_does_not_carry_its_label_fails_loud(self, tmp_path: Path) -> None:
        checkout = _checkout(tmp_path, vendored=True)
        mislabelling = _DOCKER.replace('label="${arg#TEATREE_GENERATION=}"', "label=wrong")

        run = _run(tmp_path, checkout, checkout.sha, _Host(docker=mislabelling))

        assert run.result.returncode != 0
        assert "revision label" in run.result.stderr


class TestTheBaseIsReusedAcrossCommits:
    def test_the_base_is_built_from_the_dependency_inputs_alone(self, tmp_path: Path) -> None:
        checkout = _checkout(tmp_path, vendored=True)

        run = _run(tmp_path, checkout, checkout.sha)

        assert run.result.returncode == 0, run.result.stderr
        assert set(_archive_members(run.base_context)) == {
            "pyproject.toml",
            "uv.lock",
            "vendor/teatree/pyproject.toml",
            "vendor/teatree/deploy/Dockerfile",
            "vendor/teatree/deploy/locked-version.sh",
        }

    def test_a_code_only_commit_reuses_the_base_and_builds_only_its_own_layer(self, tmp_path: Path) -> None:
        checkout = _checkout(tmp_path, vendored=True)
        _run(tmp_path, checkout, checkout.sha)
        code_only = _commit(checkout, "payload.txt", "a code change\n")

        run = _run(tmp_path, checkout, code_only)

        assert run.result.returncode == 0, run.result.stderr
        assert _builds(run, "generation-base") == []
        assert len(_builds(run, "generation")) == 1

    def test_a_lock_change_builds_a_new_base(self, tmp_path: Path) -> None:
        checkout = _checkout(tmp_path, vendored=True)
        first = _builds(_run(tmp_path, checkout, checkout.sha), "generation-base")[0]
        relocked = _commit(checkout, "uv.lock", 'version = 1\nsource = { editable = "." }\n# relocked\n')

        second = _builds(_run(tmp_path, checkout, relocked), "generation-base")

        assert len(second) == 1
        assert second[0].split(" -t ")[1] != first.split(" -t ")[1]


class TestARegistryRepository:
    _REPOSITORY = "registry.example/team/teatree-factory"

    def _host(self) -> _Host:
        return _Host(env=(("TEATREE_IMAGE_REPOSITORY", self._REPOSITORY),))

    def test_a_generation_already_in_the_registry_is_pulled_not_built(self, tmp_path: Path) -> None:
        checkout = _checkout(tmp_path, vendored=True)
        (tmp_path / "registry").mkdir()
        (tmp_path / "registry" / f"registry.example_team_teatree-factory_{checkout.sha}").write_text(checkout.sha)

        run = _run(tmp_path, checkout, checkout.sha, self._host())

        assert run.result.returncode == 0, run.result.stderr
        assert f"pull --quiet {self._REPOSITORY}:{checkout.sha}" in run.docker_calls
        assert _builds(run) == []

    def test_a_base_already_in_the_registry_is_pulled_and_only_the_thin_layer_built(self, tmp_path: Path) -> None:
        checkout = _checkout(tmp_path, vendored=True)
        (tmp_path / "probe").mkdir()
        base_tag = _builds(_run(tmp_path / "probe", checkout, checkout.sha), "generation-base")[0].split(" -t ")[1]
        base_ref = base_tag.split(" ")[0].replace("teatree-factory:", f"{self._REPOSITORY}:")
        (tmp_path / "registry").mkdir(exist_ok=True)
        (tmp_path / "registry" / base_ref.replace("/", "_").replace(":", "_")).write_text("base")

        run = _run(tmp_path, checkout, checkout.sha, self._host())

        assert run.result.returncode == 0, run.result.stderr
        assert _builds(run, "generation-base") == []
        assert f"--build-arg TEATREE_BASE_IMAGE={base_ref} " in _builds(run, "generation")[0]

    def test_a_push_run_pushes_the_base_it_built_and_the_generation(self, tmp_path: Path) -> None:
        checkout = _checkout(tmp_path, vendored=True)
        host = _Host(env=(("TEATREE_IMAGE_REPOSITORY", self._REPOSITORY), ("TEATREE_PUSH_IMAGES", "1")))

        run = _run(tmp_path, checkout, checkout.sha, host)

        pushes = [call for call in run.docker_calls if call.startswith("push ")]
        assert [call.rsplit(":", 1)[0] for call in pushes] == [f"push --quiet {self._REPOSITORY}"] * 2
        assert pushes[1].endswith(f":{checkout.sha}")


class TestOlderGenerationsAreReaped:
    @staticmethod
    def _older_generations(tmp_path: Path) -> list[str]:
        images = tmp_path / "images"
        images.mkdir()
        oldest_first = [digit * 40 for digit in "1234"]
        for age, sha in enumerate(reversed(oldest_first), start=1):
            image = images / f"teatree-factory_{sha}"
            image.write_text(sha, encoding="utf-8")
            stamp = time.time() - age * 3600
            os.utime(image, (stamp, stamp))
        return oldest_first

    @staticmethod
    def _remaining(tmp_path: Path) -> set[str]:
        names = {path.name for path in (tmp_path / "images").iterdir()}
        return {name.removeprefix("teatree-factory_") for name in names if name.startswith("teatree-factory_")}

    def test_only_images_older_than_the_newest_three_and_held_by_nothing_go(self, tmp_path: Path) -> None:
        checkout = _checkout(tmp_path, vendored=False)
        oldest_first = self._older_generations(tmp_path)

        run = _run(tmp_path, checkout, checkout.sha)

        assert run.result.returncode == 0, run.result.stderr
        generations = {name for name in self._remaining(tmp_path) if not name.startswith("base-")}
        assert generations == {checkout.sha, oldest_first[3], oldest_first[2]}
        assert f"removed teatree-factory:{oldest_first[1]}" in run.result.stdout

    def test_the_serving_generation_keeps_its_tag_though_three_newer_ones_were_built(self, tmp_path: Path) -> None:
        checkout = _checkout(tmp_path, vendored=False)
        oldest_first = self._older_generations(tmp_path)
        serving = oldest_first[0]
        (tmp_path / "images" / "teatree-headless_latest").write_text(serving, encoding="utf-8")
        ids = tmp_path / "image-ids"
        ids.write_text(
            f"teatree-factory:{serving} sha256:serving\nteatree-headless:latest sha256:serving\n", encoding="utf-8"
        )
        containers = tmp_path / "containers"
        containers.write_text("worker sha256:serving\n", encoding="utf-8")
        host = _Host(env=(("FAKE_IMAGE_IDS", str(ids)), ("FAKE_CONTAINERS", str(containers))))

        run = _run(tmp_path, checkout, checkout.sha, host)

        assert run.result.returncode == 0, run.result.stderr
        assert serving in self._remaining(tmp_path)
        assert f"removed teatree-factory:{oldest_first[1]}" in run.result.stdout

    def test_the_promoted_generation_keeps_its_tag_while_the_stack_is_down(self, tmp_path: Path) -> None:
        checkout = _checkout(tmp_path, vendored=False)
        oldest_first = self._older_generations(tmp_path)
        promoted = oldest_first[0]
        (tmp_path / "images" / "teatree-headless_latest").write_text(promoted, encoding="utf-8")
        ids = tmp_path / "image-ids"
        ids.write_text(
            f"teatree-factory:{promoted} sha256:promoted\nteatree-headless:latest sha256:promoted\n", encoding="utf-8"
        )

        run = _run(tmp_path, checkout, checkout.sha, _Host(env=(("FAKE_IMAGE_IDS", str(ids)),)))

        assert run.result.returncode == 0, run.result.stderr
        assert promoted in self._remaining(tmp_path)
