"""``DockerComposeEngine`` — each generation comes up with its own topology; the rest goes by compose labels."""

import os
import re
import stat
from pathlib import Path

import pytest

from teatree.deploy.compose_engine import DockerComposeEngine
from teatree.deploy.roll import ContainerState, RollError

N = "a" * 40


_FAKE_DOCKER = r"""#!/bin/bash
log() { printf '%s\n' "$1" >>"$FAKE_DIR/log"; }
case "$1" in
image)
    label="$(grep -F "$5=" "$FAKE_DIR/labels" 2>/dev/null | head -1 | cut -d= -f2)"
    [ -n "$label" ] || exit 1
    printf '%s\n' "$label" ;;
run)
    log "$*"
    printf '# baked %s from %s\n' "$(printf '%s' "${@: -1}" | sed 's|.*/deploy/||; s|"$||')" "${@: -3:1}" ;;
compose)
    files=""
    shift
    while [ $# -gt 0 ]; do
        case "$1" in
        -f) files="$files $(head -1 "$2")|"; shift 2 ;;
        -p | --project-directory) log "$1 $2"; shift 2 ;;
        *) break ;;
        esac
    done
    log "compose image=$TEATREE_IMAGE clone_dir=${TEATREE_CLONE_DIR:-} files=$files args=$*"
    if [ -f "$FAKE_DIR/compose_rc" ]; then echo "init: migrations failed"; exit "$(cat "$FAKE_DIR/compose_rc")"; fi ;;
ps)
    log "ps $*"
    service="$(printf '%s\n' "$@" | sed -n 's/^label=com.docker.compose.service=//p')"
    grep -F "$service=" "$FAKE_DIR/containers" 2>/dev/null | cut -d= -f2 ;;
inspect) printf 'true 2 2026-09-27T10:00:00Z %s\n' "$(cat "$FAKE_DIR/running_revision")" ;;
exec) shift; log "exec $*"; exit "$(cat "$FAKE_DIR/exec_rc" 2>/dev/null || echo 0)" ;;
*) log "$*" ;;
esac
"""


@pytest.fixture
def fake(tmp_path: Path) -> Path:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    docker = stubs / "docker"
    docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    deploy_dir = tmp_path / "checkout" / "deploy"
    deploy_dir.mkdir(parents=True)
    for name in ("docker-compose.yml", "docker-compose.host-identity.yml"):
        (deploy_dir / name).write_text(f"# checkout {name}\n", encoding="utf-8")
    (tmp_path / "log").write_text("", encoding="utf-8")
    return tmp_path


def _env(fake: Path, **env: str) -> dict[str, str]:
    return {"PATH": f"{fake / 'bin'}{os.pathsep}{os.environ['PATH']}", "FAKE_DIR": str(fake), **env}


def _engine(fake: Path, **env: str) -> DockerComposeEngine:
    return DockerComposeEngine(deploy_dir=fake / "checkout" / "deploy", env=_env(fake, **env))


def _compose_line(fake: Path) -> str:
    return next(line for line in _log(fake) if line.startswith("compose"))


def _log(fake: Path) -> list[str]:
    return (fake / "log").read_text(encoding="utf-8").splitlines()


class TestImageRevision:
    def test_reads_the_revision_label(self, fake: Path) -> None:
        (fake / "labels").write_text(f"teatree-factory:{N}={N}\n", encoding="utf-8")

        assert _engine(fake).image_revision(f"teatree-factory:{N}") == N

    @pytest.mark.parametrize("labels", ["", "teatree-factory:x=<no value>\n"])
    def test_an_absent_image_or_label_has_no_revision(self, fake: Path, labels: str) -> None:
        (fake / "labels").write_text(labels, encoding="utf-8")

        assert _engine(fake).image_revision("teatree-factory:x") == ""


class TestTopology:
    def test_an_image_generation_comes_up_with_the_topology_baked_in_its_image(self, fake: Path) -> None:
        _engine(fake).up(N, ["teatree-worker"])

        compose = _compose_line(fake)
        assert f"image=teatree-factory:{N}" in compose
        assert f"# baked docker-compose.yml from teatree-factory:{N}|" in compose
        assert compose.endswith("args=up -d --no-deps teatree-worker")
        assert f"--project-directory {fake / 'checkout' / 'deploy'}" in _log(fake)

    def test_reading_the_baked_topology_never_pulls(self, fake: Path) -> None:
        _engine(fake).up(N, ["teatree-worker"])

        reads = [line for line in _log(fake) if line.startswith("run ")]
        assert reads
        assert all(line.startswith("run --rm --pull never ") for line in reads)

    def test_an_image_generation_adds_its_own_generation_override(self, fake: Path) -> None:
        _engine(fake).up(N, ["teatree-worker"])

        assert f"# baked docker-compose.generation.yml from teatree-factory:{N}|" in _compose_line(fake)

    def test_host_identity_merges_after_the_generation_override_that_replaces_volume_lists(self, fake: Path) -> None:
        _engine(fake, TEATREE_HOST_HOME="/Users/someone").up(N, ["teatree-worker"])

        baked = re.findall(r"# baked (\S+) from", _compose_line(fake))
        assert baked == ["docker-compose.yml", "docker-compose.generation.yml", "docker-compose.host-identity.yml"]

    def test_the_legacy_stack_comes_up_with_the_checkout_topology_and_promoted_image(self, fake: Path) -> None:
        _engine(fake).up("", ["teatree-worker"])

        compose = _compose_line(fake)
        assert "image=teatree-headless:latest" in compose
        assert "# checkout docker-compose.yml|" in compose

    def test_the_legacy_stack_never_takes_the_generation_override(self, fake: Path) -> None:
        (fake / "checkout" / "deploy" / "docker-compose.generation.yml").write_text("# gen\n", encoding="utf-8")

        _engine(fake).up("", ["teatree-worker"])

        assert "# gen|" not in _compose_line(fake)

    def test_the_legacy_stack_is_given_its_clone_dir_never_the_rollers_own(self, fake: Path) -> None:
        engine = _engine(fake, TEATREE_CLONE_DIR="/home/teatree/teatree/vendor/teatree")

        engine.up("", ["teatree-worker"])

        assert "clone_dir=/home/teatree/teatree " in _compose_line(fake)

    def test_a_forks_legacy_clone_dir_reaches_the_legacy_stack(self, fake: Path) -> None:
        fork = DockerComposeEngine(
            deploy_dir=fake / "checkout" / "deploy",
            env=_env(fake),
            legacy_clone_dir="/home/teatree/teatree/vendor/teatree",
        )

        fork.up("", ["teatree-worker"])

        assert "clone_dir=/home/teatree/teatree/vendor/teatree " in _compose_line(fake)

    def test_an_image_generation_keeps_the_clone_dir_its_image_bakes(self, fake: Path) -> None:
        _engine(fake).up(N, ["teatree-worker"])

        assert "clone_dir= " in _compose_line(fake)

    def test_the_host_identity_overlay_joins_only_when_the_host_home_differs(self, fake: Path) -> None:
        _engine(fake, TEATREE_HOST_HOME="/Users/someone").up("", ["teatree-worker"])
        _engine(fake, TEATREE_HOST_HOME=str(Path.home())).up("", ["teatree-worker"])

        first, second = (line for line in _log(fake) if line.startswith("compose"))
        assert "host-identity" in first
        assert "host-identity" not in second


class TestInit:
    def test_init_runs_the_generations_one_shot_service(self, fake: Path) -> None:
        _engine(fake).run_init(N)

        compose = next(line for line in _log(fake) if line.startswith("compose"))
        assert compose.endswith("--exit-code-from teatree-init teatree-init")

    def test_a_failed_init_names_its_exit_code_and_output(self, fake: Path) -> None:
        (fake / "compose_rc").write_text("3", encoding="utf-8")

        with pytest.raises(RollError, match=r"teatree-init exited 3: init: migrations failed"):
            _engine(fake).run_init(N)


class TestContainersByLabel:
    def test_stop_targets_the_services_long_running_containers_only(self, fake: Path) -> None:
        (fake / "containers").write_text("teatree-worker=w1\nteatree-slack-listener=s1\n", encoding="utf-8")

        _engine(fake).stop(["teatree-worker", "teatree-slack-listener"])

        assert "stop w1 s1" in _log(fake)
        assert all("label=com.docker.compose.oneoff=False" in line for line in _log(fake) if line.startswith("ps"))

    def test_stopping_nothing_that_runs_is_a_no_op(self, fake: Path) -> None:
        _engine(fake).stop(["teatree-worker"])

        assert not any(line.startswith("stop") for line in _log(fake))

    def test_inspect_reports_whether_it_runs_and_which_revision(self, fake: Path) -> None:
        (fake / "containers").write_text("teatree-worker=w1\n", encoding="utf-8")
        (fake / "running_revision").write_text(N, encoding="utf-8")

        states = _engine(fake).inspect(["teatree-worker", "teatree-admin"])

        assert states == {
            "teatree-worker": ContainerState(running=True, revision=N, restarts=2, started_at="2026-09-27T10:00:00Z"),
            "teatree-admin": ContainerState(running=False, revision="", present=False),
        }

    @pytest.mark.parametrize(("exec_rc", "answers"), [("0", True), ("7", False)])
    def test_the_admin_is_probed_inside_its_own_container(self, fake: Path, exec_rc: str, *, answers: bool) -> None:
        (fake / "containers").write_text("teatree-admin=a1\n", encoding="utf-8")
        (fake / "exec_rc").write_text(exec_rc, encoding="utf-8")

        assert _engine(fake).admin_answers() is answers
        assert any(line.startswith("exec a1 curl") for line in _log(fake))


def test_the_engine_needs_the_deploy_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEATREE_DEPLOY_CHECKOUT", raising=False)

    with pytest.raises(RollError, match="TEATREE_DEPLOY_CHECKOUT"):
        DockerComposeEngine.from_environment()


class TestPromote:
    def test_the_generation_image_is_tagged_as_the_promoted_tag(self, fake: Path) -> None:
        _engine(fake).promote(N)

        assert f"tag teatree-factory:{N} teatree-headless:latest" in _log(fake)


_SECOND_STACK = {
    "TEATREE_COMPOSE_PROJECT": "zddproof",
    "TEATREE_PROMOTED_TAG": "zddproof-headless:latest",
    "TEATREE_ADMIN_PORT": "8100",
    "TEATREE_LEGACY_CLONE_DIR": "/home/teatree/teatree/vendor/teatree",
}


class TestStackIdentity:
    def _from_environment(self, monkeypatch: pytest.MonkeyPatch, fake: Path, **env: str) -> DockerComposeEngine:
        for name in (*_SECOND_STACK, "TEATREE_HOST_HOME"):
            monkeypatch.delenv(name, raising=False)
        for name, value in {**_env(fake), "TEATREE_DEPLOY_CHECKOUT": str(fake / "checkout"), **env}.items():
            monkeypatch.setenv(name, value)
        return DockerComposeEngine.from_environment()

    def test_the_defaults_are_the_live_stacks(self, monkeypatch: pytest.MonkeyPatch, fake: Path) -> None:
        engine = self._from_environment(monkeypatch, fake)

        assert (engine.project, engine.promoted_tag, engine.legacy_clone_dir) == (
            "teatree",
            "teatree-headless:latest",
            "/home/teatree/teatree",
        )
        assert engine.admin_probe_url == "http://127.0.0.1:8000/admin/login/"
        assert engine.deploy_dir == fake / "checkout" / "deploy"

    def test_a_second_stack_is_addressed_by_its_own_project_tag_and_port(
        self, monkeypatch: pytest.MonkeyPatch, fake: Path
    ) -> None:
        (fake / "containers").write_text("teatree-admin=a1\n", encoding="utf-8")
        engine = self._from_environment(monkeypatch, fake, **_SECOND_STACK)

        engine.up("", ["teatree-worker"])
        engine.admin_answers()
        engine.promote(N)

        log = _log(fake)
        assert "-p zddproof" in log
        assert "image=zddproof-headless:latest clone_dir=/home/teatree/teatree/vendor/teatree " in _compose_line(fake)
        assert all("label=com.docker.compose.project=zddproof" in line for line in log if line.startswith("ps"))
        assert "exec a1 curl -fsS -o /dev/null --max-time 5 http://127.0.0.1:8100/admin/login/" in log
        assert f"tag teatree-factory:{N} zddproof-headless:latest" in log

    def test_a_non_numeric_admin_port_is_refused(self, monkeypatch: pytest.MonkeyPatch, fake: Path) -> None:
        with pytest.raises(RollError, match="TEATREE_ADMIN_PORT"):
            self._from_environment(monkeypatch, fake, TEATREE_ADMIN_PORT="eighty")
