"""The read-only executor a managed-lane harness runs a write-barring phase in, admitted only by a live probe."""

import json
import os
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from teatree.agents import codex_read_only_probe_program
from teatree.agents.codex_auth_cache import CodexAuthCacheError
from teatree.agents.codex_read_only_verdict import (
    DECLARED_TMPFS,
    DEVICE_ROOT,
    FORBIDDEN_TOOLS,
    KERNEL_ROOTS,
    evaluate,
    planted_home,
)
from teatree.paths import data_dir_root
from teatree.utils.run import CompletedProcess, TimeoutExpired, run_allowed_to_fail
from teatree.utils.secrets import SecretStoreError, pass_entry_names

if TYPE_CHECKING:
    from teatree.agents.harness_registry import HarnessBuildContext

EXECUTOR_SERVICE: Final = "teatree-ro-executor"
EXECUTOR_PHASES: Final = frozenset({"planning"})
EXECUTOR_LOGIN_ENTRY: Final = "teatree/codex/ro-executor-auth-json-b64"
DOCKER_TIMEOUT_SECONDS: Final = 180.0

_PROBE_PROGRAM: Final = Path(codex_read_only_probe_program.__file__).read_text(encoding="utf-8")
EXECUTOR_INSPECT_FORMAT: Final = '{"Id":{{json .Id}},"Mounts":{{json .Mounts}},"HostConfig":{{json .HostConfig}}}'
WORKER_INSPECT_FORMAT: Final = (
    '{"Id":{{json .Id}},"Project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"Mounts":{{json .Mounts}},"CheckoutEnv":[{{range .Config.Env}}'
    '{{if eq (index (split . "=") 0) "TEATREE_DEPLOY_CHECKOUT"}}{{json .}}{{end}}{{end}}]}'
)
_APP_SERVER_SCRIPT: Final = 'test -d "$CODEX_HOME" && exec codex "$@"'


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

    def probe(self, container_id: str, roots: Sequence[str], home: str = "") -> object:
        request = {
            "kernel_roots": list(KERNEL_ROOTS),
            "device_root": DEVICE_ROOT,
            "tmpfs": sorted(DECLARED_TMPFS),
            "roots": list(roots),
            "home": home,
            "tools": list(FORBIDDEN_TOOLS),
        }
        return self.program(container_id, _PROBE_PROGRAM, request)


def executor_lease_dir() -> Path:
    return data_dir_root() / "codex-ro-executor"


def login_account(raw: bytes, where: str) -> str:
    """The ``tokens.account_id`` of a complete login, which also carries a ``tokens.refresh_token``."""
    try:
        tokens = json.loads(raw).get("tokens")
    except (ValueError, AttributeError):
        tokens = None
    account = tokens.get("account_id") if isinstance(tokens, dict) else None
    refresh = tokens.get("refresh_token") if isinstance(tokens, dict) else None
    if not (isinstance(account, str) and account and isinstance(refresh, str) and refresh):
        msg = f"the login in {where} lacks tokens.account_id or tokens.refresh_token"
        raise CodexAuthCacheError(msg)
    return account


def _login_refusal() -> str | None:
    prefix, _, name = EXECUTOR_LOGIN_ENTRY.rpartition("/")
    try:
        if name in pass_entry_names(prefix):
            return None
    except SecretStoreError:
        return f"the pass store could not be listed for {EXECUTOR_LOGIN_ENTRY}"
    return "it has no Codex login: run `t3 codex auth import --executor` in teatree-worker"


def executor_availability_reason(phase: str) -> str | None:
    """The cheap part of the refusal, safe to cache: it grants nothing, the live probe still decides."""
    if phase not in EXECUTOR_PHASES:
        return refusal_reason(phase, f"the read-only executor serves only {', '.join(sorted(EXECUTOR_PHASES))}")
    if refusal := _login_refusal():
        return refusal_reason(phase, refusal)
    return None


def attest(
    phase: str, roots: Sequence[str], *, home: str = "", docker: ExecutorDocker | None = None
) -> ExecutorAttestation:
    if phase not in EXECUTOR_PHASES:
        msg = f"the read-only executor serves only {', '.join(sorted(EXECUTOR_PHASES))}"
        raise ExecutorRefusalError(msg)
    if refusal := _login_refusal():
        raise ExecutorRefusalError(refusal)
    docker = docker or ExecutorDocker()
    worker = docker.worker()
    container_id = docker.executor_id(str(worker["Project"]))
    executor = docker.answer(["inspect", "--format", EXECUTOR_INSPECT_FORMAT, container_id])
    observation = docker.probe(container_id, roots, home)
    if refusal := evaluate(observation, executor, worker, roots):
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
