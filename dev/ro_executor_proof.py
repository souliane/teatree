"""Prove the read-only executor on a real container: its live verdict, the sweep, the tmpfs reset, the home mode.

Pre-merge, from a worktree (needs only docker; everything lives under DIR and is removed after):

    uv run python dev/ro_executor_proof.py scratch --dir DIR

builds the branch's ``ro-executor`` stage and runs deploy/docker-compose.yml's own executor block in a throwaway
compose project beside a mounts twin (never named teatree-worker or teatree-admin, which host hooks select by
service name), on placeholder trees. It also shows that a fresh clone volume refuses the executor until a
read-write mount has created the worktree mountpoint, which deploy.sh's init provides.

Post-deploy (A10 step c), inside teatree-worker, with the interpreter ``t3`` itself runs on:

    "$(sed -n '1s/^#!//p' "$(command -v t3)")" <deploy checkout>/dev/ro_executor_proof.py live [--root DIR ...]

runs the same checks on the deployed executor while holding the executor lease, so no planning run is in
flight; it plants nothing in the mounted trees. One JSON line per check; the exit code is 1 on any FAIL.
"""

import argparse
import fcntl
import json
import os
import shutil
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import yaml

from teatree.agents import codex_read_only_sweep_program
from teatree.agents.codex_read_only_executor import (
    EXECUTOR_INSPECT_FORMAT,
    EXECUTOR_SERVICE,
    WORKER_INSPECT_FORMAT,
    ExecutorDocker,
    executor_lease_dir,
)
from teatree.agents.codex_read_only_server import CREATE_HOME_SCRIPT, RESET_SCRIPT
from teatree.agents.codex_read_only_verdict import (
    CHECKOUT_ENV,
    CLONE_ROOT,
    EXECUTOR_HOME_ROOT,
    SCRATCH_ROOT,
    WORKTREE_ROOT,
    evaluate,
)
from teatree.utils.run import CompletedProcess, run_allowed_to_fail, run_checked

REPO = Path(__file__).resolve().parents[1]
SWEEP_PROGRAM = Path(codex_read_only_sweep_program.__file__).read_text(encoding="utf-8")
ESCAPEE = ["sleep", "1006"]
PROCESS_TABLE = (
    "import json, os, sys\n"
    "table = {}\n"
    "for name in filter(str.isdigit, os.listdir('/proc')):\n"
    "    try:\n"
    "        fields = open('/proc/%s/stat' % name).read().rsplit(')', 1)[1].split()\n"
    "        argv = [a.decode() for a in open('/proc/%s/cmdline' % name, 'rb').read().split(b'\\0') if a]\n"
    "    except OSError:\n"
    "        continue\n"
    "    if fields[0] not in ('Z', 'X'):\n"
    "        table[name] = {'ppid': int(fields[1]), 'sid': int(fields[3]), 'argv': argv}\n"
    "sys.stdout.write(json.dumps(table))\n"
)
PROJECT = "ro-executor-proof"
TWIN = "mounts-twin"
IMAGE = f"{PROJECT}-ro-executor:scratch"
BUILD_TIMEOUT_SECONDS = 3600.0


@dataclass(frozen=True)
class Executor:
    docker: ExecutorDocker
    container_id: str
    worker: object
    roots: tuple[str, ...]

    def run(self, script: str, *args: str, env: Sequence[tuple[str, str]] = ()) -> str:
        argv = self.docker.exec_argv(self.container_id, ["sh", "-c", script, "_", *args], env=env)
        return run_checked(["docker", *argv]).stdout.strip()

    def verdict(self, roots: tuple[str, ...]) -> str | None:
        executor = self.docker.answer(["inspect", "--format", EXECUTOR_INSPECT_FORMAT, self.container_id])
        return evaluate(self.docker.probe(self.container_id, roots), executor, self.worker, roots)

    def processes(self) -> dict[str, dict[str, object]]:
        table = self.docker.program(self.container_id, PROCESS_TABLE, {})
        if not isinstance(table, dict):
            msg = f"the process table answer is not an object: {table!r:.200}"
            raise TypeError(msg)
        return table


def _report(check: str, *, passed: bool, detail: object) -> bool:
    sys.stdout.write(json.dumps({"check": check, "status": "PASS" if passed else "FAIL", "detail": detail}) + "\n")
    sys.stdout.flush()
    return passed


def check_verdict(executor: Executor) -> bool:
    refusal = executor.verdict(executor.roots)
    return _report("live verdict on the real block", passed=refusal is None, detail=refusal)


def check_sweep_escape(executor: Executor) -> bool:
    """The review's escape: an env-cleared, detached child of a Codex-home exec, reparented to PID 1."""
    plant = f"({' '.join(['env', '-i', 'setsid', *ESCAPEE])} >/dev/null 2>&1 &)"
    executor.run(plant, env=(("CODEX_HOME", f"{EXECUTOR_HOME_ROOT}/codex-proof"),))
    before = executor.processes()
    escaped = [pid for pid, process in before.items() if process["argv"] == ESCAPEE]
    planted = len(escaped) == 1 and before[escaped[0]]["ppid"] == 1
    answer = executor.docker.program(executor.container_id, SWEEP_PROGRAM, {"home_root": EXECUTOR_HOME_ROOT})
    after = executor.processes()
    init = after.get("1", {})
    keeper = [pid for pid, process in after.items() if process["ppid"] == 1 and process["sid"] == init.get("sid")]
    passed = planted and answer == {"survivors": []} and escaped[0] not in after and keeper != []
    detail = {"planted": escaped, "sweep": answer, "left": sorted(after), "keeper": keeper}
    return _report("a detached env-cleared process is ended, PID 1 and its keeper live", passed=passed, detail=detail)


def check_tmpfs(executor: Executor) -> bool:
    leftovers = f"{SCRATCH_ROOT}/leftover {EXECUTOR_HOME_ROOT}/notes"
    executor.run(f"mkdir {EXECUTOR_HOME_ROOT}/codex-stale && for path in {leftovers}; do echo x >$path; done")
    executor.run(RESET_SCRIPT, EXECUTOR_HOME_ROOT, SCRATCH_ROOT)
    left = executor.run('for root; do ls -A -- "$root"; done', EXECUTOR_HOME_ROOT, SCRATCH_ROOT)
    home = f"{EXECUTOR_HOME_ROOT}/codex-proof"
    executor.run(CREATE_HOME_SCRIPT, home)
    mode = executor.run('stat -c %a -- "$1"', home)
    executor.run(RESET_SCRIPT, EXECUTOR_HOME_ROOT, SCRATCH_ROOT)
    passed = not left and mode == "700"
    return _report("both tmpfs emptied, a new home is 0700", passed=passed, detail={"left": left, "mode": mode})


@contextmanager
def executor_lease() -> Iterator[bool]:
    lease_dir = executor_lease_dir()
    lease_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(lease_dir / ".auth.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def live(roots: tuple[str, ...]) -> bool:
    docker = ExecutorDocker()
    worker = docker.worker()
    executor = Executor(docker, docker.executor_id(str(worker["Project"])), worker, roots)
    results = [check_verdict(executor)]
    with executor_lease() as held:
        if not held:
            return _report("executor lease", passed=False, detail="a server holds it; rerun when no planning runs")
        results += [check_sweep_escape(executor), check_tmpfs(executor)]
    return all(results)


@dataclass(frozen=True)
class Scratch:
    root: Path

    @property
    def checkout(self) -> Path:
        return self.root / "deploy-checkout"

    @property
    def host_home(self) -> Path:
        return self.root / "host-home"

    @property
    def worktree(self) -> Path:
        return self.host_home / "workspace" / "t3-workspaces" / "example" / "teatree"

    @property
    def compose_file(self) -> Path:
        return self.root / "compose.yml"

    def compose(self, *args: str, check: bool = True) -> CompletedProcess[str]:
        argv = ["docker", "compose", "-p", PROJECT, "-f", str(self.compose_file), *args]
        if check:
            return run_checked(argv, timeout=600)
        return run_allowed_to_fail(argv, expected_codes=None, timeout=600)

    def lay_out(self) -> None:
        os.umask(0o022)
        self.root.mkdir(parents=True, exist_ok=True)
        git = ["git", "-c", "user.name=Example", "-c", "user.email=example@example.invalid", "-C", str(self.checkout)]
        run_checked(["git", "init", "-q", str(self.checkout)])
        (self.checkout / "README").write_text("placeholder\n", encoding="utf-8")
        run_checked([*git, "add", "README"])
        run_checked([*git, "commit", "-qm", "placeholder"])
        self.worktree.parent.mkdir(parents=True)
        run_checked([*git, "worktree", "add", "-q", str(self.worktree)])
        secrets = self.checkout / "deploy" / "teatree.env"
        secrets.parent.mkdir()
        secrets.write_text("EXAMPLE_TOKEN=placeholder\n", encoding="utf-8")
        secrets.chmod(0o600)

    def write_compose(self) -> None:
        deployed = yaml.safe_load((REPO / "deploy" / "docker-compose.yml").read_text(encoding="utf-8"))
        rendered = json.dumps(deployed["services"][EXECUTOR_SERVICE])
        for variable, value in (("TEATREE_DEPLOY_CHECKOUT", self.checkout), ("TEATREE_HOST_HOME", self.host_home)):
            default = rendered.split("${" + variable + ":-", 1)[1].split("}", 1)[0]
            rendered = rendered.replace("${" + variable + ":-" + default + "}", str(value))
        executor = json.loads(rendered) | {"image": IMAGE}
        for key in ("build", "profiles"):
            executor.pop(key)
        twin = {
            "image": IMAGE,
            "user": "0:0",
            "entrypoint": ["/usr/bin/tini", "--", "sleep", "infinity"],
            "environment": {CHECKOUT_ENV: str(self.checkout)},
            "network_mode": "none",
            "volumes": [
                f"teatree_clones:{CLONE_ROOT}",
                {"type": "bind", "source": str(self.checkout), "target": str(self.checkout)},
                {
                    "type": "bind",
                    "source": str(self.host_home / "workspace" / "t3-workspaces"),
                    "target": WORKTREE_ROOT,
                },
            ],
        }
        compose = {
            "services": {EXECUTOR_SERVICE: executor, TWIN: twin},
            "networks": {"ro-executor": None},
            "volumes": {"teatree_clones": None},
        }
        self.compose_file.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")


def check_secret_control(executor: Executor, scratch: Scratch) -> bool:
    planted = scratch.worktree / ".envrc"
    planted.write_text("export EXAMPLE_TOKEN=placeholder\n", encoding="utf-8")
    try:
        refusal = executor.verdict(executor.roots)
    finally:
        planted.unlink()
    passed = refusal is not None and refusal.startswith("it can read") and ".envrc" in refusal
    return _report("control: a readable .envrc in a worktree refuses", passed=passed, detail=refusal)


def check_root_control(executor: Executor) -> bool:
    refusal = executor.verdict((WORKTREE_ROOT,))
    passed = refusal is not None and "git cannot read the checkout" in refusal
    return _report("control: a run root that is no checkout refuses", passed=passed, detail=refusal)


def scratch(root: Path) -> bool:
    if root.exists():
        msg = f"{root} exists; pass a directory that does not"
        raise SystemExit(msg)
    layout = Scratch(root)
    layout.lay_out()
    layout.write_compose()
    docker = ExecutorDocker()
    try:
        run_checked(
            ["docker", "build", "-q", "--target", "ro-executor", "-f", "deploy/Dockerfile", "-t", IMAGE, "."],
            cwd=REPO,
            timeout=BUILD_TIMEOUT_SECONDS,
        )
        fresh = layout.compose("up", "-d", EXECUTOR_SERVICE, check=False)
        refused = fresh.returncode != 0 and "read-only file system" in fresh.stderr
        detail = fresh.stderr.strip().splitlines()[-1:]
        results = [_report("control: a fresh clone volume refuses the executor", passed=refused, detail=detail)]
        layout.compose("rm", "-f", EXECUTOR_SERVICE)
        layout.compose("up", "-d", TWIN)
        layout.compose("up", "-d", EXECUTOR_SERVICE)
        twin = layout.compose("ps", "-q", TWIN).stdout.strip()
        worker = docker.answer(["inspect", "--format", WORKER_INSPECT_FORMAT, twin])
        root_in_container = f"{WORKTREE_ROOT}/example/teatree"
        executor = Executor(docker, docker.executor_id(PROJECT), worker, (root_in_container,))
        checks: list[Callable[[], bool]] = [
            lambda: check_verdict(executor),
            lambda: check_root_control(executor),
            lambda: check_secret_control(executor, layout),
            lambda: check_sweep_escape(executor),
            lambda: check_tmpfs(executor),
        ]
        results += [check() for check in checks]
    finally:
        layout.compose("down", "-v", "--remove-orphans", check=False)
        run_allowed_to_fail(["docker", "image", "rm", "-f", IMAGE], expected_codes=None)
        shutil.rmtree(root, ignore_errors=True)
    return all(results)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("scratch").add_argument("--dir", type=Path, required=True)
    modes.add_parser("live").add_argument("--root", action="append", default=[])
    args = parser.parse_args()
    passed = scratch(args.dir) if args.mode == "scratch" else live(tuple(args.root))
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
