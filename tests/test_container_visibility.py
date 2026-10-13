"""Proving a host path is reachable inside the CLI container, before claiming it.

``deploy/t3`` refuses a working directory it cannot translate into container
coordinates, and documents ``TEATREE_INVOCATION_CWD`` as the escape for a caller
that KNOWS the tree is reachable. These lanes are what turns "knows" into a proof:
a path under an identity bind mount is handed across, and everything else — a
translating mount, an unmounted tree, a daemon that will not answer — yields
nothing, so the refusal stands.

The negative lanes are the point. A prover that always says yes is a blind
``TEATREE_INVOCATION_CWD=$(pwd)``, which is exactly the leak the refusal exists to
stop. And a prover that reads ANOTHER stack's container answers a question the
launcher never asks, so the parity lane runs the launcher's own selection functions
against the same docker table the prover reads.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from _deploy_wrapper_paths import wrapper_assignment, wrapper_function

from hooks.scripts import container_visibility

_DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
_BASH = shutil.which("bash") or "bash"
_SELECTION_ENV = ("TEATREE_COMPOSE_PROJECT", "COMPOSE_PROJECT_NAME", "TEATREE_DOCKER_CLI_SERVICE")
_MOUNT_LISTING = "/srv/host/teatree-deploy\t/srv/host/teatree-deploy\n/srv/host/.local/bin\t/var/lib/teatree/host-bin\n"


@pytest.fixture(autouse=True)
def _production_defaults(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (*_SELECTION_ENV, "TEATREE_DOCKER_CLI_FALLBACK_SERVICES"):
        monkeypatch.delenv(name, raising=False)
    container_visibility.proved_pick.cache_clear()
    yield
    container_visibility.proved_pick.cache_clear()


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["docker"], returncode=returncode, stdout=stdout, stderr="")


@contextmanager
def _docker_answers(*stdouts: str | None):
    with patch.object(container_visibility, "_docker", side_effect=list(stdouts)) as docker:
        yield docker


class TestTheProbeAsksTheLaunchersQuestion:
    def test_it_lists_only_the_production_project_and_only_running_containers(self) -> None:
        with _docker_answers("abc123 teatree-worker \n", _MOUNT_LISTING) as docker:
            container_visibility.identity_mount_roots()
        listing = docker.call_args_list[0].args
        assert "label=com.docker.compose.project=teatree" in listing
        assert "status=running" in listing

    def test_the_project_follows_the_launchers_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COMPOSE_PROJECT_NAME", "named")
        monkeypatch.setenv("TEATREE_COMPOSE_PROJECT", "rolled")
        with _docker_answers("", None) as docker:
            container_visibility.cli_pick()
        assert "label=com.docker.compose.project=rolled" in docker.call_args.args

    def test_it_reads_the_table_in_the_launchers_own_format(self) -> None:
        launcher = re.search(r"--format '([^']+)'", wrapper_function("probe_running_services"))
        assert launcher is not None
        with _docker_answers("") as docker:
            container_visibility.cli_pick()
        assert docker.call_args.args[-1] == launcher.group(1)

    def test_the_route_order_is_the_launchers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert container_visibility.cli_services() == ("teatree-worker", "teatree-admin")
        monkeypatch.setenv("TEATREE_DOCKER_CLI_SERVICE", "teatree-admin")
        monkeypatch.setenv("TEATREE_DOCKER_CLI_FALLBACK_SERVICES", "teatree-admin teatree-worker extra")
        assert container_visibility.cli_services() == ("teatree-admin", "teatree-worker", "extra")


class TestCliPick:
    def test_the_worker_wins_over_the_admin(self) -> None:
        with _docker_answers("adm111 teatree-admin \nwrk222 teatree-worker \n"):
            assert container_visibility.cli_pick().container == "wrk222"

    def test_a_down_worker_falls_back_to_the_admin(self) -> None:
        with _docker_answers("adm111 teatree-admin \nwatch333 teatree-watchdog \n"):
            assert container_visibility.cli_pick().container == "adm111"

    def test_an_ephemeral_one_off_is_never_the_pick(self) -> None:
        """Compose lists a `run --rm` under the service name; `exec` never lands there."""
        with _docker_answers("oneoff1 teatree-worker True\nservice1 teatree-worker \n"):
            assert container_visibility.cli_pick().container == "service1"

    def test_an_absent_oneoff_label_is_kept(self) -> None:
        with _docker_answers("service1 teatree-worker\n"):
            assert container_visibility.cli_pick().container == "service1"

    def test_a_non_cli_service_is_named_as_no_route(self) -> None:
        with _docker_answers("watch333 teatree-watchdog \n"):
            pick = container_visibility.cli_pick()
        assert pick.container == ""
        assert pick.reason == "compose project 'teatree' runs no teatree-worker or teatree-admin container"

    def test_several_containers_of_the_route_declare_nothing_and_say_why(self) -> None:
        with _docker_answers("wrk111 teatree-worker \nwrk222 teatree-worker \nadm333 teatree-admin \n"):
            pick = container_visibility.cli_pick()
        assert pick.container == ""
        assert "2 teatree-worker containers (wrk111, wrk222)" in pick.reason
        assert "scaled or started outside the deploy" in pick.reason

    def test_a_daemon_that_will_not_answer_is_named(self) -> None:
        with _docker_answers(None):
            pick = container_visibility.cli_pick()
        assert pick.container == ""
        assert pick.reason.startswith("docker gave no answer about compose project 'teatree'")
        for cause in ("no docker binary", "daemon that is down", "permission error", "timeout"):
            assert cause in pick.reason


class TestIdentityMountRoots:
    def test_a_mount_landing_on_its_own_path_is_a_root(self) -> None:
        with _docker_answers("abc123 teatree-worker \n", _MOUNT_LISTING):
            assert container_visibility.identity_mount_roots() == ("/srv/host/teatree-deploy",)

    def test_a_translating_mount_is_not(self) -> None:
        """`/srv/host/.local/bin` is real inside the container — under another name."""
        with _docker_answers("abc123 teatree-worker \n", _MOUNT_LISTING):
            assert "/srv/host/.local/bin" not in container_visibility.identity_mount_roots()

    def test_the_mounts_read_are_the_picked_containers(self) -> None:
        with _docker_answers("oneoff1 teatree-worker True\nservice1 teatree-worker \n", _MOUNT_LISTING) as docker:
            container_visibility.identity_mount_roots()
        assert docker.call_args.args[-1] == "service1"

    def test_a_daemon_that_will_not_answer_proves_nothing(self) -> None:
        with _docker_answers(None):
            assert container_visibility.identity_mount_roots() == ()

    def test_an_unreadable_mount_table_proves_nothing_and_says_why(self) -> None:
        with _docker_answers("abc123 teatree-worker \n", None):
            assert container_visibility.identity_mount_roots() == ()
            assert "the mounts of container abc123" in (
                container_visibility.unreachable_reason(Path("/srv/host/teatree-deploy")) or ""
            )

    def test_no_running_service_container_proves_nothing(self) -> None:
        with _docker_answers("oneoff1 teatree-worker True\n"):
            assert container_visibility.identity_mount_roots() == ()


class TestDockerProbe:
    def test_an_absent_docker_binary_answers_nothing(self) -> None:
        with patch.object(container_visibility.shutil, "which", return_value=None):
            assert container_visibility._docker("ps") is None

    def test_a_failed_probe_answers_nothing(self) -> None:
        with (
            patch.object(container_visibility.shutil, "which", return_value="/usr/bin/docker"),
            patch("subprocess.run", return_value=_completed("boom", returncode=1)),
        ):
            assert container_visibility._docker("ps") is None

    def test_a_probe_that_never_returns_answers_nothing(self) -> None:
        with (
            patch.object(container_visibility.shutil, "which", return_value="/usr/bin/docker"),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 5)),
        ):
            assert container_visibility._docker("ps") is None


class TestContainerPath:
    def test_a_checkout_under_an_identity_mount_crosses_unchanged(self) -> None:
        with _docker_answers("abc123 teatree-worker \n", _MOUNT_LISTING):
            assert container_visibility.container_path(Path("/srv/host/teatree-deploy")) == ("/srv/host/teatree-deploy")

    def test_a_tree_nested_under_one_crosses_too(self) -> None:
        with _docker_answers("abc123 teatree-worker \n", _MOUNT_LISTING):
            assert container_visibility.container_path(Path("/srv/host/teatree-deploy/hooks")) == (
                "/srv/host/teatree-deploy/hooks"
            )

    def test_an_unmounted_tree_is_not_vouched_for(self) -> None:
        with _docker_answers("abc123 teatree-worker \n", _MOUNT_LISTING):
            assert container_visibility.container_path(Path("/srv/host/elsewhere/repo")) is None

    def test_a_tree_under_a_translating_mount_is_not_vouched_for(self) -> None:
        """It IS readable inside — under a different name, which is not what we hand over."""
        with _docker_answers("abc123 teatree-worker \n", _MOUNT_LISTING):
            assert container_visibility.container_path(Path("/srv/host/.local/bin/t3")) is None

    def test_it_vouches_for_the_physical_path_a_mount_source_is(self, tmp_path) -> None:
        real = (tmp_path / "real").resolve()
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        with _docker_answers("abc123 teatree-worker \n", f"{real}\t{real}\n"):
            assert container_visibility.container_path(link) == str(real)


class TestUnreachableReason:
    def test_a_proven_path_has_no_reason(self) -> None:
        with _docker_answers("abc123 teatree-worker \n", _MOUNT_LISTING):
            assert container_visibility.unreachable_reason(Path("/srv/host/teatree-deploy/hooks")) is None

    def test_a_mount_mismatch_points_at_the_plugin_installation(self) -> None:
        with _docker_answers("abc123 teatree-worker \n", _MOUNT_LISTING):
            reason = container_visibility.unreachable_reason(Path("/srv/host/elsewhere/repo")) or ""
        assert "container abc123 of compose project 'teatree'" in reason
        assert "/srv/host/elsewhere/repo" in reason
        assert "plugin" in reason
        assert "deploy" not in reason.replace("teatree-deploy", "")

    def test_no_pick_carries_the_picks_reason(self) -> None:
        with _docker_answers("wrk111 teatree-worker \nwrk222 teatree-worker \n"):
            reason = container_visibility.unreachable_reason(Path("/srv/host/teatree-deploy")) or ""
        assert "scaled or started outside the deploy" in reason


_FAKE_DOCKER = """\
import json, os, sys
args = sys.argv[1:]
rows = json.loads(os.environ["FAKE_DOCKER_ROWS"])
if args[0] == "ps":
    if args[args.index("--format") + 1] != os.environ["FAKE_DOCKER_PS_FORMAT"]:
        sys.exit("fake docker: a format the launcher does not use")
    wanted = [args[i + 1] for i, arg in enumerate(args) if arg == "--filter"]
    for row in rows:
        labels = {
            "com.docker.compose.project": row["project"],
            "com.docker.compose.service": row["service"],
            **({"com.docker.compose.oneoff": row["oneoff"]} if row.get("oneoff") else {}),
        }
        def matches(spec):
            kind, _, value = spec.partition("=")
            if kind == "status":
                return row["status"] == value
            key, sep, expected = value.partition("=")
            return key in labels and (not sep or labels[key] == expected)
        if all(matches(spec) for spec in wanted):
            print(row["id"], labels["com.docker.compose.service"], labels.get("com.docker.compose.oneoff", ""))
elif args[0] == "inspect":
    row = next((row for row in rows if row["id"] == args[-1]), None)
    if row is None:
        sys.exit(1)
    for source, destination in row.get("mounts", []):
        print(f"{source}\\t{destination}")
"""


def _row(container: str, project: str, service: str, *, oneoff: str = "", status: str = "running") -> dict:
    return {"id": container, "project": project, "service": service, "oneoff": oneoff, "status": status}


_PROD_WORKER = _row("prod1", "teatree", "teatree-worker")
_PROD_ADMIN = _row("prod2", "teatree", "teatree-admin")
_ONE_OFF_WORKER = _row("one1", "teatree", "teatree-worker", oneoff="True")
_FOREIGN_WORKER = _row("scratch1", "scratch", "teatree-worker")

_PARITY_TABLES = [
    pytest.param([_FOREIGN_WORKER, _PROD_WORKER], {}, "prod1", id="foreign_worker_listed_first"),
    pytest.param([_FOREIGN_WORKER], {}, "", id="only_a_foreign_worker"),
    pytest.param([{**_PROD_WORKER, "status": "exited"}, _PROD_ADMIN], {}, "prod2", id="worker_down_admin_up"),
    pytest.param([_row("prod9", "teatree", "teatree-watchdog")], {}, "", id="a_non_cli_service_only"),
    pytest.param([_ONE_OFF_WORKER, _PROD_WORKER], {}, "prod1", id="a_one_off_beside_the_service"),
    pytest.param([_ONE_OFF_WORKER, _PROD_ADMIN], {}, "prod2", id="a_one_off_worker_and_an_admin"),
    pytest.param(
        [{**_PROD_WORKER, "status": "paused"}, {**_PROD_ADMIN, "status": "created"}], {}, "", id="nothing_running"
    ),
    pytest.param(
        [_PROD_WORKER, _row("zdd1", "zddproof", "teatree-worker")],
        {"TEATREE_COMPOSE_PROJECT": "zddproof"},
        "zdd1",
        id="a_rolled_project",
    ),
    pytest.param(
        [_PROD_WORKER, _row("alt1", "alt", "teatree-worker")],
        {"COMPOSE_PROJECT_NAME": "alt"},
        "alt1",
        id="a_named_project",
    ),
    pytest.param(
        [_PROD_WORKER, _PROD_ADMIN], {"TEATREE_DOCKER_CLI_SERVICE": "teatree-admin"}, "prod2", id="a_redirected_service"
    ),
]


class TestParityWithTheLauncher:
    """The hook and ``deploy/t3``'s own selection functions, one docker table, one answer."""

    @pytest.fixture
    def docker_table(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        fake = tmp_path / "bin" / "docker"
        fake.parent.mkdir()
        fake.write_text(f"#!{sys.executable}\n{_FAKE_DOCKER}", encoding="utf-8")
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("FAKE_DOCKER_PS_FORMAT", container_visibility.PROBE_FORMAT)

        def serve(rows: list[dict], env: dict[str, str]) -> None:
            monkeypatch.setenv("FAKE_DOCKER_ROWS", json.dumps(rows))
            for name, value in env.items():
                monkeypatch.setenv(name, value)

        return serve

    @staticmethod
    def _launcher_pick() -> str:
        script = "\n".join(
            [
                "set -euo pipefail",
                f'. "{_DEPLOY}/generation-topology.sh"',
                wrapper_assignment("SERVICE"),
                wrapper_assignment("CLI_SERVICE_FALLBACKS"),
                wrapper_assignment("COMPOSE_PROJECT"),
                *(wrapper_function(name) for name in ("probe_running_services", "running_service_container")),
                wrapper_function("first_running_service"),
                'RUNNING_SERVICE_TABLE="$(probe_running_services)"',
                'svc="$(first_running_service)" || exit 0',
                'running_service_container "$svc"',
            ]
        )
        result = subprocess.run([_BASH, "-c", script], capture_output=True, text=True, timeout=60, check=False)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    @pytest.mark.parametrize(("rows", "env", "expected"), _PARITY_TABLES)
    def test_both_pick_the_same_container(self, docker_table, rows: list[dict], env: dict, expected: str) -> None:
        docker_table(rows, env)
        assert (container_visibility.cli_pick().container, self._launcher_pick()) == (expected, expected)

    def test_a_foreign_stack_alone_is_named_by_the_production_project_only(self, docker_table) -> None:
        docker_table([_FOREIGN_WORKER], {})
        reason = container_visibility.unreachable_reason(Path("/srv/host/teatree-deploy")) or ""
        assert "compose project 'teatree'" in reason
        assert "scratch" not in reason

    def test_a_foreign_worker_listed_first_does_not_hide_the_checkout(self, docker_table, tmp_path: Path) -> None:
        checkout = (tmp_path / "checkout").resolve()
        checkout.mkdir()
        docker_table([_FOREIGN_WORKER, {**_PROD_WORKER, "mounts": [[str(checkout), str(checkout)]]}], {})
        assert container_visibility.container_path(checkout) == str(checkout)

    def test_several_routes_are_where_the_hook_refuses_to_guess(self, docker_table) -> None:
        """The launcher execs into one of them arbitrarily; the deploy never runs two."""
        docker_table([_row("wrk1", "teatree", "teatree-worker"), _row("wrk2", "teatree", "teatree-worker")], {})
        assert self._launcher_pick() in {"wrk1", "wrk2"}
        assert container_visibility.cli_pick().container == ""
