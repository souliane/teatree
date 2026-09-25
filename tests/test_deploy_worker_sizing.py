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

from teatree.utils.ram_probe import DockerWorkerSizing, available_cpu_count, host_total_ram_mib

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "deploy"
COMPOSE_FILE = DEPLOY / "docker-compose.yml"
DEPLOY_SH = DEPLOY / "deploy.sh"
FF_CHECKOUT_SH = DEPLOY / "fast-forward-checkout.sh"
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
        # The derivation must precede the staged convergence it feeds.
        assert text.index("compose-sizing") < text.index("staged_swap || {")


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TestDeployShRunDerivesWorkerCaps:
    """The REAL deploy.sh under tmp_path exports the host-derived worker cap.

    Every external command is stubbed, and the value deploy.sh exports into
    `docker compose up` must equal the cap ram_probe derives here.
    """

    def _stage(self, tmp_path: Path) -> tuple[Path, Path, Path, dict[str, str], int, int]:
        repo = tmp_path / "repo"
        (repo / "deploy").mkdir(parents=True)
        (repo / "src" / "teatree" / "utils").mkdir(parents=True)
        shutil.copy(DEPLOY_SH, repo / "deploy" / "deploy.sh")
        # deploy.sh delegates the fast-forward to this sibling; staging deploy.sh
        # alone makes the run die at `No such file or directory` before it reaches
        # the compose invocation under test.
        shutil.copy(FF_CHECKOUT_SH, repo / "deploy" / "fast-forward-checkout.sh")
        # Host pressure installation is outside this sizing test and must not
        # register a real launchd agent on the macOS test host.
        _write_exec(repo / "deploy" / "install-host-pressure.zsh", "#!/bin/zsh\nexit 0\n")
        shutil.copy(RAM_PROBE, repo / "src" / "teatree" / "utils" / "ram_probe.py")
        (repo / "deploy" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (repo / "deploy" / "teatree.env").write_text("", encoding="utf-8")

        record_cpus = tmp_path / "recorded_cpus"
        record_mem = tmp_path / "recorded_mem"
        physical_ram_mib = host_total_ram_mib()
        daemon_ram_mib = max(1, physical_ram_mib // 2)
        daemon_cpus = max(1, available_cpu_count() // 2)
        bindir = tmp_path / "bin"
        bindir.mkdir()
        _write_exec(
            bindir / "docker",
            "#!/usr/bin/env bash\n"
            'for a in "$@"; do\n'
            '  case "$a" in\n'
            '    info) case "$*" in\n'
            f'      *NCPU*) printf %s "{daemon_cpus}";;\n'
            f'      *) printf %s "{daemon_ram_mib * 1024 * 1024}";;\n'
            "    esac; exit 0;;\n"
            f'    up) printf %s "$TEATREE_WORKER_CPUS" > "{record_cpus}"; '
            f'printf %s "$TEATREE_WORKER_MEM_LIMIT" > "{record_mem}"; exit 0;;\n'
            "    exec) echo '{\"running\": true}'; exit 0;;\n"
            # The staged swap polls init's terminal state before it swaps anything.
            "    ps) echo stubcid; exit 0;;\n"
            "    inspect)\n"
            '      case "$*" in\n'
            "        *State.ExitCode*) echo 'exited 0';;\n"
            "        *State.Status*RestartCount*) echo 'running/0';;\n"
            "        *State.Status*) echo running;;\n"
            "      esac\n"
            "      exit 0;;\n"
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
        # Bounded so a stub gap surfaces as a fast failure, never a 30-min poll.
        env["TEATREE_INIT_WAIT_TIMEOUT"] = "5"
        env["TEATREE_ADMIN_SWAP_BUDGET"] = "5"
        env["TEATREE_RESUME_TIMEOUT"] = "5"
        # Per-test lock: deploy.sh serialises convergences on ONE global lock dir, so two
        # staged runs in the same xdist session make the second exit early having reached
        # nothing under test.
        env["TEATREE_DEPLOY_LOCK"] = str(tmp_path / "deploy.lock")
        return repo, record_cpus, record_mem, env, physical_ram_mib, daemon_ram_mib

    def test_run_exports_host_derived_cpus_into_compose_up(self, tmp_path: Path) -> None:
        repo, record_cpus, record_mem, env, physical_ram_mib, daemon_ram_mib = self._stage(tmp_path)
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
        assert record_cpus.exists(), f"the compose convergence was never reached:\n{proc.stdout}\n{proc.stderr}"
        # deploy.sh runs uncapped here just as on the host; the value it exported is
        # exactly what ram_probe derives in-process from the host and the stub engine.
        assert record_cpus.read_text() == str(
            DockerWorkerSizing.worker_cpus(daemon_cpus=max(1, available_cpu_count() // 2))
        )
        assert daemon_ram_mib < physical_ram_mib
        expected_mem = DockerWorkerSizing.worker_mem_limit_mib(
            total_ram_mib=physical_ram_mib,
            daemon_ram_mib=daemon_ram_mib,
        )
        if expected_mem > 0:
            assert record_mem.read_text() == f"{expected_mem}m"

    def _stage_with_stub_sizer(self, tmp_path: Path, body: str) -> tuple[Path, Path, dict[str, str]]:
        """Stage the real deploy.sh against a DETERMINISTIC sizer.

        The sibling test copies the real ``ram_probe.py``, so what it exercises depends on
        the RAM of whatever box runs it. These two exercise the branch, so the sizer is a
        stub and the outcome is the same everywhere.
        """
        repo, record_cpus, _record_mem, env, _physical_ram_mib, _daemon_ram_mib = self._stage(tmp_path)
        # deploy.sh runs this file with `python3 <path>`, so the stub is PYTHON — a shell
        # stub would raise SyntaxError and silently exercise the degrade branch instead.
        (repo / "src" / "teatree" / "utils" / "ram_probe.py").write_text(body, encoding="utf-8")
        return repo, record_cpus, env

    def _run(self, repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        bash = shutil.which("bash")
        assert bash is not None
        return subprocess.run(
            [bash, str(repo / "deploy" / "deploy.sh")],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )

    def test_an_operator_exported_cpu_cap_reaches_compose_up(self, tmp_path: Path) -> None:
        """RED before the fix: the sizer's derived line was eval'd over the operator's value."""
        repo, record_cpus, _record_mem, env, _physical_ram_mib, _daemon_ram_mib = self._stage(tmp_path)
        env["TEATREE_WORKER_CPUS"] = "2"

        proc = self._run(repo, env)

        assert proc.returncode == 0, f"deploy.sh failed:\n{proc.stdout}\n{proc.stderr}"
        assert record_cpus.read_text() == "2"

    def test_a_refusing_sizer_aborts_the_deploy_with_the_reason(self, tmp_path: Path) -> None:
        """Exit 3 means no workable cap exists — abort, and print the remedy.

        RED before #151: deploy.sh discarded both the exit code (``|| true``) and stderr
        (``2>/dev/null``), so a refusal was indistinguishable from success and the stack
        came up on compose's 18g default — effectively uncapped on the very VM that could
        not hold a 6 GiB one.
        """
        repo, record_cpus, env = self._stage_with_stub_sizer(
            tmp_path,
            "import sys\n"
            'sys.stdout.write("TEATREE_WORKER_CPUS=7\\n")\n'
            'sys.stderr.write("raise Docker memory allocation to at least 9473 MiB\\n")\n'
            "raise SystemExit(3)\n",
        )

        proc = self._run(repo, env)

        assert proc.returncode != 0, f"a refusal must abort the deploy:\n{proc.stdout}\n{proc.stderr}"
        assert "9473" in proc.stderr, proc.stderr
        assert not record_cpus.exists(), "the stack was converged despite an unusable worker cap"

    def test_a_sizer_that_merely_fails_still_degrades_to_the_compose_default(self, tmp_path: Path) -> None:
        """TOO-STRICT control: the new branch is exit-3 only, never 'abort on any failure'.

        A missing python3 / an import error must keep the pre-existing silent degrade, or
        every box without a usable interpreter loses the ability to deploy at all.
        """
        repo, record_cpus, env = self._stage_with_stub_sizer(
            tmp_path,
            'import sys\nsys.stderr.write("ImportError: no module named teatree\\n")\nraise SystemExit(1)\n',
        )

        proc = self._run(repo, env)

        assert proc.returncode == 0, f"a failed probe must not abort:\n{proc.stdout}\n{proc.stderr}"
        assert record_cpus.exists(), "the deploy stopped instead of degrading to the compose default"


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
