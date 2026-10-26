"""A fake ``docker`` CLI on PATH that plays the worker's own container and the read-only executor."""

import base64
import copy
import json
import os
import signal
import socket
import stat
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from string import Template
from typing import Final, TypedDict

import pytest

from teatree.agents import codex_read_only_server
from teatree.agents.codex_auth_cache import CODEX_AUTH_PASS_ENTRY
from teatree.agents.codex_read_only_executor import EXECUTOR_LOGIN_ENTRY, ExecutorAttestation, attest
from teatree.agents.codex_read_only_probe_program import Mount, Observation
from teatree.agents.codex_read_only_verdict import (
    CLONE_ROOT,
    CODEX_HOME_ROOT,
    CODEX_SOURCE_GIT,
    EXECUTOR_HOME_ROOT,
    SCRATCH_ROOT,
    WORKTREE_ROOT,
    codex_source_git_subpath,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "codex_read_only_executor"
CONTAINER_HOME = str(PurePosixPath(CLONE_ROOT).parent)
EXAMPLE_ROOT = f"{WORKTREE_ROOT}/example/teatree"
EXECUTOR_ID = "container-01"

_SCRIPT = r"""
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CONFIG = json.loads(open(CONFIG_PATH, encoding="utf-8").read())
argv = sys.argv[1:]
with open(CONFIG["log"], "a", encoding="utf-8") as log:
    log.write(json.dumps(argv) + "\n")


def alive(pid):
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except (OSError, IndexError):
        return False


def ours(pid):
    try:
        with open("/proc/%d/environ" % pid, "rb") as environ:
            return ("FAKE_EXECUTOR_CONFIG=" + CONFIG_PATH).encode() in environ.read().split(b"\0")
    except OSError:
        return False


def keeper():
    if not os.path.exists(CONFIG["keeper"]):
        return None
    with open(CONFIG["keeper"], encoding="utf-8") as handle:
        return json.load(handle)


def started():
    if not os.path.exists(CONFIG["apps"]):
        return []
    with open(CONFIG["apps"], encoding="utf-8") as handle:
        return [int(line) for line in handle.read().split()]


def container_proc(root, kept):
    # The executor's /proc as the sweep sees it: tini as PID 1, its sleep, and what this fake started.
    if kept is not None:
        os.symlink("/proc/%d" % kept["init"], root + "/1")
        os.symlink("/proc/%d" % kept["sleep"], root + "/%d" % kept["sleep"])
    for pid in started():
        if ours(pid) and not os.path.exists(root + "/%d" % pid):
            os.symlink("/proc/%d" % pid, root + "/%d" % pid)

if argv[:1] == ["inspect"]:
    name = argv[-1]
    if CONFIG.get("inspect_exit"):
        sys.stderr.write("Cannot connect to the Docker daemon\n")
        sys.exit(CONFIG["inspect_exit"])
    if name == CONFIG["hostname"]:
        sys.stdout.write(json.dumps(CONFIG["worker"]))
    elif name in CONFIG["containers"]:
        sys.stdout.write(json.dumps(CONFIG["executor"]))
    else:
        sys.stderr.write("Error: No such object: " + name + "\n")
        sys.exit(1)
elif argv[:1] == ["ps"]:
    sys.stdout.write("".join(cid + "\n" for cid in CONFIG["containers"]))
elif argv[:1] == ["exec"]:
    rest = argv[1:]
    env = {}
    while rest and rest[0].startswith("-"):
        if rest[0] == "-e":
            key, _, value = rest[1].partition("=")
            env[key] = value
            rest = rest[2:]
        else:
            rest = rest[1:]
    container, command = rest[0], rest[1:]
    if container == CONFIG["worker"]["Id"] and command == ["test", "-d", CONFIG["codex_source_git"]]:
        if CONFIG["codex_source_git_error"]:
            sys.stderr.write("Error response from daemon: worker stopped\n")
            sys.exit(1)
        sys.exit(0 if CONFIG["codex_source_git_present"] else 1)
    if container not in CONFIG["containers"]:
        sys.stderr.write("Error: No such container: " + container + "\n")
        sys.exit(1)
    kept = keeper()
    if kept is not None and not alive(kept["sleep"]):
        # tini exits with its only child, and the container's tmpfs goes with it.
        shutil.rmtree(CONFIG["fs"], ignore_errors=True)
        sys.stderr.write("Error response from daemon: container " + container + " is not running\n")
        sys.exit(1)
    if command[:1] == ["/usr/bin/python3"] and "home_root" in json.loads(command[-1]):
        program = sys.stdin.read()
        with open(CONFIG["lease"], "a") as lease, open(CONFIG["log"] + ".sweeps", "a", encoding="utf-8") as sweeps:
            try:
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                sweeps.write("leased\n")
            else:
                fcntl.flock(lease.fileno(), fcntl.LOCK_UN)
                sweeps.write("unleased\n")
        if CONFIG["raw_sweep"] is not None:
            raw = CONFIG["raw_sweep"]
            sys.stdout.write(raw if isinstance(raw, str) else json.dumps(raw))
            sys.exit(0)
        root = tempfile.mkdtemp(dir=CONFIG["root"])
        try:
            container_proc(root, kept)
            namespace = {"__name__": "executor_sweep"}
            exec(compile(program, "executor_sweep", "exec"), namespace)
            answer = namespace["sweep"](json.loads(command[-1]), Path(root))
        finally:
            shutil.rmtree(root)
        sys.stdout.write(json.dumps(answer))
        sys.exit(0)
    if command[:1] == ["/usr/bin/python3"]:
        sys.stdin.read()
        with open(CONFIG["log"] + ".probe", "a", encoding="utf-8") as probes:
            probes.write(command[-1] + "\n")
        answer = CONFIG["probe"]
        request = json.loads(command[-1])
        if isinstance(answer, dict) and isinstance(answer.get("roots"), dict):
            missing = {"exists": False, "is_dir": False, "write": "ENOENT", "git": "ENOENT"}
            answer["roots"] = {root: answer["roots"].get(root, missing) for root in request["roots"]}
        if isinstance(answer, dict) and request["home"]:
            live = CONFIG["fs"] + request["home"]
            rules = live + "/rules"
            answer["home"] = {
                "rules": sorted(n for n in os.listdir(rules) if n.endswith(".rules")) if os.path.isdir(rules) else [],
                "config": os.path.exists(live + "/config.toml"),
                "plugins": os.path.exists(live + "/plugins"),
            }
        sys.stdout.write(answer if isinstance(answer, str) else json.dumps(answer))
        sys.exit(CONFIG["probe_exit"])
    def mapped(path):
        return CONFIG["fs"] + path

    if command[:4] == ["setsid", "-w", "sh", "-c"]:
        if not os.path.isdir(mapped(env["CODEX_HOME"])):
            sys.exit(1)
        with open(CONFIG["log"] + ".env", "a", encoding="utf-8") as env_log:
            # PEP 538 locale coercion adds LC_CTYPE to this Python process; a Go docker CLI never does.
            coerced = {"LC_CTYPE"} if os.environ.get("LC_CTYPE") == "C.UTF-8" else set()
            env_log.write(json.dumps(sorted(set(os.environ) - coerced)) + "\n")
        os.setsid()
        with open(CONFIG["apps"], "a", encoding="utf-8") as apps:
            apps.write("%d\n" % os.getpid())
        child_env = {
            "CODEX_HOME": env["CODEX_HOME"],
            "FAKE_FS": CONFIG["fs"],
            "FAKE_EXECUTOR_CONFIG": CONFIG_PATH,
            "PATH": os.environ.get("PATH", ""),
        }
        codex_flags = ["--disable", "plugins"] if "codex --disable plugins" in command[4] else []
        os.execve(sys.executable, [sys.executable, CONFIG["app_server"], *codex_flags, *command[6:]], child_env)
    if command[:2] == ["sh", "-c"] and command[2] == CONFIG["scripts"]["list_homes"]:
        root = mapped(CONFIG["home_root"])
        for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
            if name.startswith("codex-") and os.path.isdir(root + "/" + name):
                print(CONFIG["home_root"] + "/" + name)
        sys.exit(0)
    if command[:2] == ["sh", "-c"] and command[2] in CONFIG["scripts"]["path_args"]:
        # The core's own script, run by a real sh against this fake's filesystem.
        script = ["sh", "-c", command[2], command[3], *(mapped(path) for path in command[4:])]
        sys.exit(subprocess.run(script, check=False).returncode)
    sys.stderr.write("fake docker: unhandled exec " + json.dumps(command) + "\n")
    sys.exit(2)
else:
    sys.stderr.write("fake docker: unhandled " + json.dumps(argv) + "\n")
    sys.exit(2)
"""

_PASS = r"""
import json
import os
import sys

store = json.loads(open(STORE_PATH, encoding="utf-8").read())
with open(STORE_PATH + ".log", "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1:2] == ["show"]:
    if sys.argv[2] not in store:
        sys.stderr.write("Error: " + sys.argv[2] + " is not in the password store.\n")
        sys.exit(1)
    print(store[sys.argv[2]])
elif sys.argv[1:2] == ["insert"]:
    if os.path.exists(STORE_PATH + ".readonly"):
        sys.exit(1)
    store[sys.argv[-1]] = sys.stdin.read().strip()
    open(STORE_PATH, "w", encoding="utf-8").write(json.dumps(store))
else:
    sys.exit(2)
"""

_APP_SERVER = r"""
import json
import os
import sys

config = json.loads(open(os.environ["FAKE_EXECUTOR_CONFIG"], encoding="utf-8").read())
home = os.environ["FAKE_FS"] + os.environ["CODEX_HOME"]
threads = 0
if sys.argv[1:3] != ["--disable", "plugins"]:
    os.mkdir(home + "/plugins")


def emit(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


for raw in sys.stdin:
    request = json.loads(raw)
    with open(config["log"] + ".rpc", "a", encoding="utf-8") as log:
        log.write(json.dumps(request) + "\n")
    method, request_id = request.get("method"), request.get("id")
    if request_id is None or method is None:
        continue
    if method == "initialize":
        emit({"id": request_id, "result": {"userAgent": "fake-executor"}})
    elif method == "account/read":
        emit({"id": request_id, "result": {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True}})
    elif method == "thread/start":
        threads += 1
        emit({"id": request_id, "result": {"thread": {"id": "thread-%d" % threads}, "model": "gpt-fake"}})
    elif method == "mcpServerStatus/list":
        emit({"id": request_id, "result": {"data": []}})
    elif method == "turn/start":
        thread_id = request["params"]["threadId"]
        emit({"id": request_id, "result": {"turn": {"id": "turn-1", "items": [], "status": "inProgress"}}})
        with open(config["log"] + ".scratch", "a", encoding="utf-8") as scratch:
            scratch.write(json.dumps(sorted(os.listdir(config["scratch"]))) + "\n")
        with open(config["scratch"] + "/turn-note", "w", encoding="utf-8") as note:
            note.write(thread_id)
        rotated = config["rotated_login"]
        if rotated is not None:
            with open(home + "/auth.json", "w", encoding="utf-8") as auth:
                auth.write(rotated if isinstance(rotated, str) else json.dumps(rotated))
        if config["plant_plugins_after_turn"]:
            os.mkdir(home + "/plugins")
        common = {"threadId": thread_id, "turnId": "turn-1"}
        item = {"id": "msg-1", "type": "agentMessage", "text": config["reply"]}
        emit({"method": "item/completed", "params": common | {"completedAtMs": 1, "item": item}})
        done = {"id": "turn-1", "items": [], "status": "completed"}
        emit({"method": "turn/completed", "params": common | {"turn": done}})
    elif method == "thread/unsubscribe":
        emit({"id": request_id, "result": {}})
    else:
        emit({"id": request_id, "error": {"code": -32601, "message": "unsupported"}})
"""

EXECUTOR_ACCOUNT: Final = "acct-example"
STORED_LOGIN: Final = {
    "tokens": {"account_id": EXECUTOR_ACCOUNT, "refresh_token": "stored"},
    "last_refresh": "2026-10-01T00:00:00Z",
}
WORKER_LOGIN: Final = {
    "tokens": {"account_id": "acct-worker", "refresh_token": "worker"},
    "last_refresh": "2026-10-11T00:00:00Z",
}
ROTATED_LOGIN: Final = {
    "tokens": {"account_id": EXECUTOR_ACCOUNT, "refresh_token": "rotated"},
    "last_refresh": "2026-10-12T09:00:00Z",
}
_KEEPER = (
    "import subprocess, sys\n"
    "child = subprocess.Popen(['sleep', '600'])\n"
    "print(child.pid, flush=True)\n"
    "sys.exit(child.wait())\n"
)


class ThreadStart(TypedDict):
    cwd: str
    sandbox: str
    approvalPolicy: str
    config: dict[str, dict[str, object]]


class DockerMount(TypedDict, total=False):
    Type: str
    Name: str
    Source: str
    Destination: str
    RW: bool


class ExecutorInspect(TypedDict):
    Id: str
    Mounts: list[DockerMount]
    HostConfig: dict[str, object]


class WorkerInspect(TypedDict):
    Id: str
    Project: str
    Mounts: list[DockerMount]
    CheckoutEnv: list[str]


def _fixture_text(name: str) -> str:
    template = Template((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return template.substitute(
        CONTAINER_HOME=CONTAINER_HOME,
        CODEX_HOME_ROOT=CODEX_HOME_ROOT,
        CODEX_SOURCE_GIT=CODEX_SOURCE_GIT,
        SOURCE_GIT_SUBPATH=codex_source_git_subpath(),
    )


def probe_fixture(name: str) -> Observation:
    answer: Observation = json.loads(_fixture_text(f"{name}_probe"))
    return answer


def executor_fixture(name: str) -> ExecutorInspect:
    answer: ExecutorInspect = json.loads(_fixture_text(f"{name}_inspect"))
    return answer


def worker_fixture() -> WorkerInspect:
    answer: WorkerInspect = json.loads(_fixture_text("worker_inspect"))
    return answer


def mount(target: str, fstype: str = "ext4", options: str = "ro,relatime") -> Mount:
    return Mount(target=target, fstype=fstype, options=options, source="/dev/disk0")


@dataclass
class FakeExecutor:
    """Edit ``probe``/``executor``/``worker``/``containers`` before :meth:`install`, then read :attr:`calls`."""

    root: Path
    probe: Observation = field(default_factory=lambda: probe_fixture("pass"))
    raw_probe: object = None
    executor: ExecutorInspect = field(default_factory=lambda: executor_fixture("pass"))
    worker: WorkerInspect = field(default_factory=worker_fixture)
    containers: list[str] = field(default_factory=lambda: [EXECUTOR_ID])
    probe_exit: int = 0
    inspect_exit: int = 0
    with_login: bool = True
    reply: str = "OK"
    plant_plugins_after_turn: bool = False
    codex_source_git_present: bool = True
    codex_source_git_error: bool = False
    rotated_login: object = None
    stored_login: dict[str, object] = field(default_factory=lambda: dict(STORED_LOGIN))
    raw_sweep: object = None
    _processes: list[subprocess.Popen[str]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.probe = copy.deepcopy(self.probe)
        self.executor["Id"] = EXECUTOR_ID

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"

    @property
    def store(self) -> Path:
        return self.root / "password-store"

    @property
    def log(self) -> Path:
        return self.root / "docker.log"

    @property
    def fs(self) -> Path:
        """Where the executor's ``/run/t3-ro`` tmpfs lives for this fake."""
        return self.root / "executor-fs"

    @property
    def pass_store(self) -> Path:
        return self.root / "pass.json"

    @property
    def config_path(self) -> Path:
        return self.root / "docker-config.json"

    @property
    def keeper_path(self) -> Path:
        return self.root / "keeper.json"

    @property
    def keeper_alive(self) -> bool:
        sleep = json.loads(self.keeper_path.read_text(encoding="utf-8"))["sleep"]
        try:
            state = Path(f"/proc/{sleep}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return False
        return state not in {"Z", "X"}

    @contextmanager
    def running(self) -> Generator[None]:
        """The container's keeper: a tini-like PID 1 whose ``sleep`` child the container lives and dies with."""
        keeper = self._spawn([sys.executable, "-c", _KEEPER], {})
        try:
            assert keeper.stdout is not None
            sleep = int(keeper.stdout.readline())
            self.keeper_path.write_text(json.dumps({"init": keeper.pid, "sleep": sleep}), encoding="utf-8")
            for tmpfs in (EXECUTOR_HOME_ROOT, SCRATCH_ROOT):
                Path(f"{self.fs}{tmpfs}").mkdir(parents=True, exist_ok=True)
            yield
        finally:
            for process in self._processes:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                if process.stdout is not None:
                    process.stdout.close()

    def stray(self, name: str) -> subprocess.Popen[str]:
        """A Codex App Server process a dead server left running in the executor."""
        process = self._spawn(
            [sys.executable, "-c", "import time; time.sleep(600)"], {"CODEX_HOME": f"{EXECUTOR_HOME_ROOT}/codex-{name}"}
        )
        with Path(self.root / "apps").open("a", encoding="utf-8") as apps:
            apps.write(f"{process.pid}\n")
        return process

    def _spawn(self, argv: list[str], env: dict[str, str]) -> subprocess.Popen[str]:
        marker = {"FAKE_EXECUTOR_CONFIG": str(self.config_path), "PATH": os.environ.get("PATH", "")}
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True, env=marker | env, start_new_session=True)
        self._processes.append(process)
        return process

    def homes(self) -> list[Path]:
        root = Path(f"{self.fs}{EXECUTOR_HOME_ROOT}")
        return sorted(root.glob("codex-*")) if root.is_dir() else []

    def stored(self, entry: str) -> dict[str, object]:
        value = json.loads(self.pass_store.read_text(encoding="utf-8"))[entry]
        return json.loads(base64.b64decode(value))

    @property
    def pass_entries(self) -> set[str]:
        """Every pass entry a ``show`` or an ``insert`` named."""
        log = Path(f"{self.pass_store}.log")
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return {json.loads(line)[-1] for line in lines}

    @property
    def pass_writes(self) -> list[str]:
        log = Path(f"{self.pass_store}.log")
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return [json.loads(line)[-1] for line in lines if json.loads(line)[0] == "insert"]

    @property
    def thread_starts(self) -> list[ThreadStart]:
        log = Path(f"{self.log}.rpc")
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        starts: list[ThreadStart] = [
            json.loads(line)["params"] for line in lines if json.loads(line).get("method") == "thread/start"
        ]
        return starts

    @property
    def sweep_leases(self) -> list[str]:
        """``leased`` or ``unleased`` per sweep: whether the executor lease was held while it ran."""
        log = Path(f"{self.log}.sweeps")
        return log.read_text(encoding="utf-8").split() if log.exists() else []

    @property
    def scratch_at_turns(self) -> list[list[str]]:
        """What ``TMPDIR`` held when each turn started."""
        log = Path(f"{self.log}.scratch")
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    def tmpfs(self, root: str) -> list[str]:
        return sorted(entry.name for entry in Path(f"{self.fs}{root}").iterdir())

    @property
    def docker_cli_env(self) -> list[list[str]]:
        log = Path(f"{self.log}.env")
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    def install(self) -> dict[str, str]:
        """Write the fake and return the env a test applies: PATH first, and the pass store it stats."""
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        config = self.config_path
        config.write_text(
            json.dumps(
                {
                    "root": str(self.root),
                    "keeper": str(self.keeper_path),
                    "apps": str(self.root / "apps"),
                    "raw_sweep": self.raw_sweep,
                    "log": str(self.log),
                    "hostname": socket.gethostname(),
                    "worker": self.worker,
                    "executor": self.executor,
                    "containers": self.containers,
                    "probe": self.probe if self.raw_probe is None else self.raw_probe,
                    "probe_exit": self.probe_exit,
                    "inspect_exit": self.inspect_exit,
                    "fs": str(self.fs),
                    "home_root": EXECUTOR_HOME_ROOT,
                    "app_server": str(self.root / "app_server.py"),
                    "reply": self.reply,
                    "plant_plugins_after_turn": self.plant_plugins_after_turn,
                    "codex_source_git": f"{CODEX_HOME_ROOT}/{codex_source_git_subpath()}",
                    "codex_source_git_present": self.codex_source_git_present,
                    "codex_source_git_error": self.codex_source_git_error,
                    "rotated_login": self.rotated_login,
                    "lease": str(self.root / "data" / "codex-ro-executor" / ".auth.lock"),
                    "scratch": str(self.fs) + SCRATCH_ROOT,
                    "scripts": {
                        "list_homes": codex_read_only_server.LIST_HOMES_SCRIPT,
                        "path_args": [
                            codex_read_only_server.WRITE_AUTH_SCRIPT,
                            codex_read_only_server.READ_AUTH_SCRIPT,
                            codex_read_only_server.RESET_SCRIPT,
                            codex_read_only_server.CREATE_HOME_SCRIPT,
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        (self.root / "app_server.py").write_text(_APP_SERVER, encoding="utf-8")
        self._executable("docker", f"CONFIG_PATH = {str(config)!r}\n{_SCRIPT}")
        self._executable("pass", f"STORE_PATH = {str(self.pass_store)!r}\n{_PASS}")
        if not self.pass_store.exists():
            logins = {EXECUTOR_LOGIN_ENTRY: self.stored_login, CODEX_AUTH_PASS_ENTRY: WORKER_LOGIN}
            encoded = {key: base64.b64encode(json.dumps(login).encode()).decode() for key, login in logins.items()}
            self.pass_store.write_text(json.dumps(encoded), encoding="utf-8")
        worker_entry = self.store / f"{CODEX_AUTH_PASS_ENTRY}.gpg"
        worker_entry.parent.mkdir(parents=True, exist_ok=True)
        worker_entry.write_bytes(b"encrypted")
        if self.with_login:
            (self.store / f"{EXECUTOR_LOGIN_ENTRY}.gpg").write_bytes(b"encrypted")
        return {
            "PATH": f"{self.bin_dir}:/usr/bin:/bin",
            "PASSWORD_STORE_DIR": str(self.store),
            "T3_DATA_DIR": str(self.root / "data"),
            "TEATREE_CODEX_SOURCE_GIT_SUBPATH": codex_source_git_subpath(),
        }

    def _executable(self, name: str, body: str) -> None:
        script = self.bin_dir / name
        script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

    @property
    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    @property
    def probe_requests(self) -> list[dict[str, object]]:
        probes = Path(f"{self.log}.probe")
        if not probes.exists():
            return []
        return [json.loads(line) for line in probes.read_text(encoding="utf-8").splitlines()]


def attested(fake: FakeExecutor, monkeypatch: pytest.MonkeyPatch, roots: tuple[str, ...] = ()) -> ExecutorAttestation:
    """A real attestation from a passing probe of the fake executor, with its env applied."""
    for key, value in fake.install().items():
        monkeypatch.setenv(key, value)
    return attest("planning", roots)
