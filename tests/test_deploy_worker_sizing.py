"""Deploy-time worker sizing derives the compose CPU/RAM caps from the real host (#3432).

The worker reads the cgroup-capped CPU/RAM view, so host-derived provision
concurrency is a no-op unless the cgroup cap itself reflects the host. These tests
pin the wiring end to end: ``deploy/docker-compose.yml`` interpolates
``TEATREE_WORKER_CPUS`` / ``TEATREE_WORKER_MEM_LIMIT`` (with the pre-#3432 defaults
as fallback), and ``deploy/deploy.sh`` — run for real under ``tmp_path`` with the
external commands stubbed — derives those values from the host via
``src/teatree/utils/ram_probe.py`` and exports them into ``docker compose up``.
"""

# test-path: cross-cutting -- exercises deploy/deploy.sh + docker-compose.yml + ram_probe together
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from teatree.utils.ram_probe import derive_worker_cpus, derive_worker_mem_limit_mib

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "deploy"
COMPOSE_FILE = DEPLOY / "docker-compose.yml"
DEPLOY_SH = DEPLOY / "deploy.sh"
RAM_PROBE = REPO_ROOT / "src" / "teatree" / "utils" / "ram_probe.py"


class TestComposeInterpolatesDerivedCaps:
    def test_worker_cpus_and_mem_limit_are_env_interpolated(self) -> None:
        worker = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))["services"]["teatree-worker"]
        # The deploy-derived value wins; the pre-#3432 hard-coded caps are the fallback.
        assert worker["cpus"] == "${TEATREE_WORKER_CPUS:-3.0}"
        assert worker["mem_limit"] == "${TEATREE_WORKER_MEM_LIMIT:-18g}"


class TestDeploySizingWiring:
    def test_deploy_sh_derives_and_exports_before_compose_up(self) -> None:
        text = DEPLOY_SH.read_text(encoding="utf-8")
        assert "ram_probe.py" in text
        assert "compose-sizing" in text
        assert "export TEATREE_WORKER_CPUS TEATREE_WORKER_MEM_LIMIT" in text
        # The derivation must precede the `docker compose up` it feeds.
        assert text.index("compose-sizing") < text.index("up -d --build")


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TestDeployShRunDerivesWorkerCaps:
    """The REAL deploy.sh under tmp_path exports the host-derived worker cap.

    Every external command is stubbed, and the value deploy.sh exports into
    `docker compose up` must equal the cap ram_probe derives here.
    """

    def _stage(self, tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str]]:
        repo = tmp_path / "repo"
        (repo / "deploy").mkdir(parents=True)
        (repo / "src" / "teatree" / "utils").mkdir(parents=True)
        shutil.copy(DEPLOY_SH, repo / "deploy" / "deploy.sh")
        shutil.copy(RAM_PROBE, repo / "src" / "teatree" / "utils" / "ram_probe.py")
        (repo / "deploy" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (repo / "deploy" / "teatree.env").write_text("", encoding="utf-8")

        record_cpus = tmp_path / "recorded_cpus"
        record_mem = tmp_path / "recorded_mem"
        bindir = tmp_path / "bin"
        bindir.mkdir()
        _write_exec(
            bindir / "docker",
            "#!/usr/bin/env bash\n"
            'for a in "$@"; do\n'
            '  case "$a" in\n'
            f'    up) printf %s "$TEATREE_WORKER_CPUS" > "{record_cpus}"; '
            f'printf %s "$TEATREE_WORKER_MEM_LIMIT" > "{record_mem}"; exit 0;;\n'
            "    exec) echo '{\"running\": true}'; exit 0;;\n"
            "  esac\n"
            "done\n"
            "exit 0\n",
        )
        _write_exec(
            bindir / "git",
            '#!/usr/bin/env bash\ncase "$*" in\n  *abbrev-ref*) echo main;;\n  *short*) echo abc1234;;\nesac\nexit 0\n',
        )
        _write_exec(bindir / "systemctl", "#!/usr/bin/env bash\nexit 0\n")
        _write_exec(bindir / "curl", "#!/usr/bin/env bash\nexit 0\n")
        _write_exec(bindir / "sudo", '#!/usr/bin/env bash\nexec "$@"\n')

        env = os.environ.copy()
        env["PATH"] = f"{bindir}:{env['PATH']}"
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        env["HOME"] = str(home)
        return repo, record_cpus, record_mem, env

    def test_run_exports_host_derived_cpus_into_compose_up(self, tmp_path: Path) -> None:
        repo, record_cpus, record_mem, env = self._stage(tmp_path)
        bash = shutil.which("bash")
        assert bash is not None
        proc = subprocess.run(
            [bash, str(repo / "deploy" / "deploy.sh")],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )
        assert proc.returncode == 0, f"deploy.sh failed:\n{proc.stdout}\n{proc.stderr}"
        assert record_cpus.exists(), f"docker compose up was never reached:\n{proc.stdout}\n{proc.stderr}"
        # deploy.sh runs uncapped here just as on the host; the value it exported is
        # exactly what ram_probe derives in-process — the host-sized worker cap.
        assert record_cpus.read_text() == str(derive_worker_cpus())
        expected_mem = derive_worker_mem_limit_mib()
        if expected_mem > 0:
            assert record_mem.read_text() == f"{expected_mem}m"


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
class TestComposeConfigRendersDerivedCaps:
    def test_injected_env_reaches_the_worker_service(self) -> None:
        env = os.environ.copy()
        env["TEATREE_WORKER_CPUS"] = "7"
        env["TEATREE_WORKER_MEM_LIMIT"] = "20000m"
        docker = shutil.which("docker")
        assert docker is not None
        proc = subprocess.run(
            [docker, "compose", "-f", str(COMPOSE_FILE), "config"],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            pytest.skip(f"docker compose config unavailable: {proc.stderr}")
        rendered = yaml.safe_load(proc.stdout)["services"]["teatree-worker"]
        assert str(rendered["cpus"]) in {"7", "7.0"}
