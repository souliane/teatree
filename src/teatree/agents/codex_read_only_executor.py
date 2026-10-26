"""The read-only executor a managed-lane harness runs a write-barring phase in, admitted only by a live probe."""

import json
import os
import socket
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, NamedTuple

from teatree.agents import codex_read_only_probe_program
from teatree.agents.codex_auth_cache import CodexAuthCacheError
from teatree.agents.codex_read_only_verdict import (
    CLONE_ROOT,
    CODEX_HOME_ROOT,
    CODEX_SOURCE_GIT,
    DECLARED_TMPFS,
    DEVICE_ROOT,
    FORBIDDEN_TOOLS,
    KERNEL_ROOTS,
    WORKTREE_ROOT,
    MalformedAnswerError,
    codex_source_git_subpath,
    evaluate,
    planted_home,
)
from teatree.paths import data_dir_root
from teatree.utils.run import CompletedProcess, TimeoutExpired, run_allowed_to_fail
from teatree.utils.secrets import SecretStoreError, pass_entry_names

if TYPE_CHECKING:
    from teatree.agents.harness_registry import HarnessBuildContext

EXECUTOR_SERVICE: Final = "teatree-ro-executor"
EXECUTOR_PHASES: Final = frozenset({"planning", "reviewing", "critic_reviewing"})
EXECUTOR_LOGIN_ENTRY: Final = "teatree/codex/ro-executor-auth-json-b64"
DOCKER_TIMEOUT_SECONDS: Final = 180.0

_PROBE_PROGRAM: Final = Path(codex_read_only_probe_program.__file__).read_text(encoding="utf-8")
EXECUTOR_INSPECT_FORMAT: Final = '{"Id":{{json .Id}},"Mounts":{{json .Mounts}},"HostConfig":{{json .HostConfig}}}'
WORKER_INSPECT_FORMAT: Final = (
    '{"Id":{{json .Id}},"Project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"Mounts":{{json .Mounts}},"CheckoutEnv":[{{range .Config.Env}}'
    '{{if eq (index (split . "=") 0) "TEATREE_DEPLOY_CHECKOUT"}}{{json .}}{{end}}{{end}}]}'
)
_APP_SERVER_SCRIPT: Final = 'test -d "$CODEX_HOME" && exec codex --disable plugins "$@"'


class ExecutorRefusalError(RuntimeError):
    pass


class PlantedHomeError(ExecutorRefusalError):
    pass


class _Issue:
    """Spent by the one attestation built with it, so a replace() cannot re-issue it for another executor."""

    __slots__ = ("spent",)

    def __init__(self) -> None:
        self.spent = False


@dataclass(frozen=True, slots=True)
class ExecutorAttestation:
    container_id: str
    phase: str
    probed_at: datetime
    token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.token, _Issue) or self.token.spent:
            msg = "an ExecutorAttestation is issued only by a passing live probe"
            raise TypeError(msg)
        self.token.spent = True


class ExecutorDocker:
    """The docker CLI as the worker reaches the executor: a PATH-only environment and bounded calls."""

    @staticmethod
    def run(argv: Sequence[str], *, stdin: str | None = None) -> CompletedProcess[str]:
        try:
            return run_allowed_to_fail(
                ["docker", *argv],
                expected_codes=None,
                env={"PATH": os.environ.get("PATH", "")},
                stdin_text=stdin,
                timeout=DOCKER_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            msg = "the docker CLI is not installed here; only teatree-worker reaches the executor"
            raise ExecutorRefusalError(msg) from exc
        except TimeoutExpired as exc:
            msg = f"docker {argv[0]} did not answer within {DOCKER_TIMEOUT_SECONDS:.0f}s"
            raise ExecutorRefusalError(msg) from exc

    def answer(self, argv: Sequence[str], *, stdin: str | None = None) -> object:
        result = self.run(argv, stdin=stdin)
        if result.returncode != 0:
            detail = (result.stderr.strip().splitlines() or ["no detail"])[-1]
            msg = f"docker {argv[0]} exited {result.returncode}: {detail}"
            raise ExecutorRefusalError(msg)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            msg = f"docker {argv[0]} answered something that is not JSON"
            raise ExecutorRefusalError(msg) from exc

    @staticmethod
    def exec_argv(container_id: str, command: Sequence[str], *, env: Sequence[tuple[str, str]] = ()) -> list[str]:
        assignments = [item for key, value in env for item in ("-e", f"{key}={value}")]
        return ["exec", "-i", *assignments, container_id, *command]

    def app_server_argv(self, container_id: str, home: str) -> list[str]:
        # setsid without -w forks and returns at once from a group leader, ending the exec's stdio.
        command = ["setsid", "-w", "sh", "-c", _APP_SERVER_SCRIPT, "sh"]
        return ["docker", *self.exec_argv(container_id, command, env=(("CODEX_HOME", home), ("HOME", home)))]

    def worker(self) -> Mapping[str, object]:
        answer = self.answer(["inspect", "--format", WORKER_INSPECT_FORMAT, socket.gethostname()])
        if not isinstance(answer, dict) or not isinstance(project := answer.get("Project"), str) or not project:
            msg = "this process is not running in a compose service container"
            raise ExecutorRefusalError(msg)
        return answer

    def codex_source_git_present(self, worker_id: str) -> bool:
        try:
            subpath = codex_source_git_subpath()
        except MalformedAnswerError as exc:
            msg = f"the worker's Codex source Git path is malformed ({exc})"
            raise ExecutorRefusalError(msg) from exc
        if not subpath:
            return False
        result = self.run(self.exec_argv(worker_id, ["test", "-d", f"{CODEX_HOME_ROOT}/{subpath}"]))
        if result.returncode not in {0, 1} or result.stderr.strip():
            msg = "the worker's Codex source Git path could not be checked"
            raise ExecutorRefusalError(msg)
        return result.returncode == 0

    def executor_id(self, project: str) -> str:
        filters = (
            f"label=com.docker.compose.project={project}",
            f"label=com.docker.compose.service={EXECUTOR_SERVICE}",
        )
        result = self.run(["ps", *(item for value in filters for item in ("--filter", value)), "--format", "{{.ID}}"])
        ids = result.stdout.split()
        if result.returncode != 0 or len(ids) != 1:
            msg = f"{len(ids)} running {EXECUTOR_SERVICE} containers in compose project {project!r}"
            raise ExecutorRefusalError(msg)
        return ids[0]

    def program(self, container_id: str, source: str, request: Mapping[str, object]) -> object:
        command = ["/usr/bin/python3", "-I", "-S", "-", json.dumps(request)]
        return self.answer(self.exec_argv(container_id, command), stdin=source)

    def probe(
        self, container_id: str, roots: Sequence[str], home: str = "", *, git_mounts: Sequence[str] = ()
    ) -> object:
        request = {
            "kernel_roots": list(KERNEL_ROOTS),
            "device_root": DEVICE_ROOT,
            "tmpfs": sorted(DECLARED_TMPFS),
            "roots": list(roots),
            "git_mounts": list(git_mounts),
            "home": home,
            "tools": list(FORBIDDEN_TOOLS),
        }
        return self.program(container_id, _PROBE_PROGRAM, request)


def executor_lease_dir() -> Path:
    return data_dir_root() / "codex-ro-executor"


class Login(NamedTuple):
    """The fields of an ``auth.json`` the core decides on, ``""`` for any that is absent or malformed."""

    account: str
    refresh_token: str
    last_refresh: str

    @classmethod
    def parse(cls, raw: bytes) -> "Login":
        try:
            login = json.loads(raw)
        except ValueError:
            login = None
        login = login if isinstance(login, dict) else {}
        tokens = tokens if isinstance(tokens := login.get("tokens"), dict) else {}
        fields = (tokens.get("account_id"), tokens.get("refresh_token"), login.get("last_refresh"))
        return cls(*(value if isinstance(value, str) else "" for value in fields))


def login_account(raw: bytes, where: str) -> str:
    """The ``tokens.account_id`` of a complete login, which also carries a ``tokens.refresh_token``."""
    login = Login.parse(raw)
    if not (login.account and login.refresh_token):
        msg = f"the login in {where} lacks tokens.account_id or tokens.refresh_token"
        raise CodexAuthCacheError(msg)
    return login.account


def _login_refusal() -> str | None:
    prefix, _, name = EXECUTOR_LOGIN_ENTRY.rpartition("/")
    try:
        if name in pass_entry_names(prefix):
            return None
    except SecretStoreError:
        return f"the pass store could not be listed for {EXECUTOR_LOGIN_ENTRY}"
    return "it has no Codex login: run `t3 codex auth import --executor` in teatree-worker"


def _unserved(phase: str) -> str | None:
    if phase not in EXECUTOR_PHASES:
        return f"the read-only executor serves only {', '.join(sorted(EXECUTOR_PHASES))}"
    return _login_refusal()


def executor_availability_reason(phase: str) -> str | None:
    """The cheap part of the refusal, safe to cache: it grants nothing, the live probe still decides."""
    refusal = _unserved(phase)
    return refusal_reason(phase, refusal) if refusal else None


def _grant_git_read(path: Path, mode: int, required: int) -> None:
    if mode & required == required:
        return
    try:
        path.chmod(mode | required)
    except FileNotFoundError:
        return
    except PermissionError:
        try:
            current = path.lstat().st_mode
        except FileNotFoundError:
            return
        if current & required != required:
            raise


def _readable_git_file(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if info.st_mode & stat.S_IROTH:
        return
    if info.st_nlink > 1:
        msg = f"Git metadata {path} has links outside its checkout"
        raise ExecutorRefusalError(msg)
    _grant_git_read(path, info.st_mode, stat.S_IROTH)


def _readable_git_tree(gitdir: Path) -> None:
    pending = [gitdir]
    while pending:
        path = pending.pop()
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(mode):
            continue
        _grant_git_read(path, mode, stat.S_IROTH | stat.S_IXOTH)
        try:
            with os.scandir(path) as entries:
                children = list(entries)
        except FileNotFoundError:
            continue
        for entry in children:
            try:
                if entry.is_dir(follow_symlinks=False) and not Path(entry.path).is_mount():
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    _readable_git_file(Path(entry.path))
            except FileNotFoundError:
                continue


def repair_git_metadata(checkout: Path, *, allowed_roots: Sequence[Path]) -> None:
    """Make only resolved Git metadata readable by the unprivileged executor."""
    allowed = tuple(root.resolve() for root in allowed_roots if root.is_dir() and not root.is_symlink())
    checkout_path = checkout.resolve()
    if not any(checkout_path.is_relative_to(root) or root.is_relative_to(checkout_path) for root in allowed):
        msg = f"Git checkout {checkout} is outside the executor's declared mounts"
        raise ExecutorRefusalError(msg)
    try:
        for gitdir in codex_read_only_probe_program.resolved_git_dirs(str(checkout)):
            if not any(gitdir.is_relative_to(root) for root in allowed):
                msg = f"Git metadata {gitdir} is outside the executor's declared mounts"
                raise ExecutorRefusalError(msg)
            _readable_git_tree(gitdir)
    except OSError as exc:
        msg = f"could not make Git metadata readable for {checkout}: {exc}"
        raise ExecutorRefusalError(msg) from exc


def _repair_mounted_git(worker: Mapping[str, object], executor: object, roots: Sequence[str]) -> None:
    if not isinstance(executor, Mapping):
        return
    worker_mounts = worker.get("Mounts")
    executor_mounts = executor.get("Mounts")
    if not isinstance(worker_mounts, list) or not isinstance(executor_mounts, list):
        return

    def mounted(target: str, mounts: list[object], *, writable: bool) -> Mapping[str, object] | None:
        return next(
            (
                mount
                for mount in mounts
                if isinstance(mount, Mapping) and mount.get("Destination") == target and mount.get("RW") is writable
            ),
            None,
        )

    def same_source(target: str) -> bool:
        writable = mounted(target, worker_mounts, writable=True)
        readonly = mounted(target, executor_mounts, writable=False)
        return bool(
            writable
            and readonly
            and writable.get("Type") == readonly.get("Type")
            and writable.get("Source") == readonly.get("Source")
        )

    allowed = [Path(target) for target in (CLONE_ROOT, WORKTREE_ROOT) if same_source(target)]
    worker_codex = mounted(CODEX_HOME_ROOT, worker_mounts, writable=True)
    subpath = codex_source_git_subpath()
    source_git_target = str(Path(CODEX_HOME_ROOT) / subpath) if subpath else ""
    executor_git = mounted(source_git_target, executor_mounts, writable=False) if source_git_target else None
    if (
        worker_codex is not None
        and executor_git is not None
        and worker_codex.get("Type") == executor_git.get("Type") == "volume"
        and str(Path(str(worker_codex.get("Source"))) / subpath) == executor_git.get("Source")
    ):
        allowed.append(Path(source_git_target))
    checkouts = [
        Path(root) for root in roots if Path(root).is_dir() and any(Path(root).is_relative_to(a) for a in allowed)
    ]

    checkout_env = worker.get("CheckoutEnv")
    if isinstance(checkout_env, list):
        named = next(
            (
                entry.partition("=")[2]
                for entry in checkout_env
                if isinstance(entry, str) and entry.startswith("TEATREE_DEPLOY_CHECKOUT=")
            ),
            "",
        )
        deploy = Path(named) if named else None
        if deploy is not None and deploy.is_dir() and (deploy / ".git").is_dir():
            worker_deploy = mounted(str(deploy), worker_mounts, writable=True)
            executor_git = mounted(str(deploy / ".git"), executor_mounts, writable=False)
            if (
                worker_deploy is not None
                and executor_git is not None
                and worker_deploy.get("Type") == executor_git.get("Type") == "bind"
                and str(Path(str(worker_deploy.get("Source"))) / ".git") == executor_git.get("Source")
            ):
                allowed.append(deploy / ".git")
                checkouts.append(deploy)
    for checkout in checkouts:
        repair_git_metadata(checkout, allowed_roots=allowed)


def attest(
    phase: str, roots: Sequence[str], *, home: str = "", docker: ExecutorDocker | None = None
) -> ExecutorAttestation:
    if refusal := _unserved(phase):
        raise ExecutorRefusalError(refusal)
    docker = docker or ExecutorDocker()
    worker = docker.worker()
    worker_id = worker.get("Id")
    if not isinstance(worker_id, str) or not worker_id:
        reason = "the worker container ID is unreadable"
        raise ExecutorRefusalError(reason)
    codex_source_git_present = docker.codex_source_git_present(worker_id)
    container_id = docker.executor_id(str(worker["Project"]))
    executor = docker.answer(["inspect", "--format", EXECUTOR_INSPECT_FORMAT, container_id])
    _repair_mounted_git(worker, executor, roots)
    mounts = executor.get("Mounts") if isinstance(executor, Mapping) else None
    git_mounts = (
        [
            target
            for mount in mounts
            if isinstance(mount, Mapping)
            and isinstance(target := mount.get("Destination"), str)
            and (target == CODEX_SOURCE_GIT or target.endswith("/.git"))
        ]
        if isinstance(mounts, list)
        else []
    )
    observation = docker.probe(container_id, roots, home, git_mounts=git_mounts)
    if refusal := evaluate(observation, executor, worker, roots, codex_source_git_present=codex_source_git_present):
        msg = f"{EXECUTOR_SERVICE} is not read-only: {refusal}"
        raise ExecutorRefusalError(msg)
    if planted := planted_home(observation):
        raise PlantedHomeError(planted)
    return ExecutorAttestation(container_id, phase, datetime.now(UTC), _Issue())


def refusal_reason(phase: str, detail: object) -> str:
    return f"phase {phase!r} bars write tools, which a managed-lane harness cannot enforce: {detail}"


def read_only_executor_refusal(context: "HarnessBuildContext") -> str | None:
    phase = context.phase or ""
    try:
        attest(phase, ())
    except ExecutorRefusalError as exc:
        return refusal_reason(phase, exc)
    return None
