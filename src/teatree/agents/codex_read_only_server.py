"""Codex App Server inside the read-only executor: own login and lease, a fresh home per server, a probe per thread."""

import asyncio
import atexit
import json
import os
import secrets
import threading
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents import codex_read_only_sweep_program
from teatree.agents.codex_app_server_lifecycle import opened_session
from teatree.agents.codex_app_server_options import CodexAppServerError, CodexAppServerOptions, workspace_roots
from teatree.agents.codex_auth_cache import CodexAuthCache, CodexAuthCacheError, encode_auth_cache
from teatree.agents.codex_read_only_executor import (
    EXECUTOR_LOGIN_ENTRY,
    ExecutorAttestation,
    ExecutorDocker,
    ExecutorRefusalError,
    PlantedHomeError,
    attest,
    executor_lease_dir,
    login_account,
    refusal_reason,
)
from teatree.agents.codex_read_only_verdict import EXECUTOR_HOME_ROOT
from teatree.agents.codex_shared_app_server import SharedCodexAppServer, SharedCodexSession
from teatree.agents.harness_options import HarnessOptions
from teatree.agents.harness_registry import HarnessFallbackError, HarnessFallbackKind
from teatree.utils.run import CompletedProcess

WRITE_AUTH_SCRIPT: Final = 'umask 077; mkdir -p "$1" && cat >"$1/.auth.tmp" && mv "$1/.auth.tmp" "$1/auth.json"'
LIST_HOMES_SCRIPT: Final = f'for home in {EXECUTOR_HOME_ROOT}/codex-*; do [ -d "$home" ] && echo "$home"; done; true'
_SWEEP_PROGRAM: Final = Path(codex_read_only_sweep_program.__file__).read_text(encoding="utf-8")


def end_codex_processes(docker: ExecutorDocker, container_id: str) -> None:
    """Under the lease no other server runs, so every process a Codex home started is a stray."""
    answer = docker.program(container_id, _SWEEP_PROGRAM, {"home_root": EXECUTOR_HOME_ROOT})
    if answer != {"survivors": []}:
        msg = f"the sweep did not end every stray Codex process in the executor ({answer!r:.200})"
        raise ExecutorRefusalError(msg)


class ExecutorAuthFile:
    """``auth.json`` in an executor home, moved through ``docker exec`` stdin and stdout only.

    The executor's agent can rewrite its home, so a read returns only a login of *account*.
    """

    def __init__(self, docker: ExecutorDocker, container_id: str, home: str, *, account: str = "") -> None:
        self.docker = docker
        self.container_id = container_id
        self.home = home
        self.account = account

    def run(self, command: Sequence[str], *, stdin: str | None = None) -> CompletedProcess[str]:
        try:
            return self.docker.run(self.docker.exec_argv(self.container_id, command), stdin=stdin)
        except ExecutorRefusalError as exc:
            raise CodexAuthCacheError(str(exc)) from exc

    def read(self) -> bytes:
        result = self.run(["cat", f"{self.home}/auth.json"])
        if result.returncode != 0:
            msg = f"the executor auth file could not be read (exit {result.returncode})"
            raise CodexAuthCacheError(msg)
        raw = result.stdout.encode()
        if login_account(raw, self.home) != self.account:
            msg = f"the login in {self.home} is not the executor's account"
            raise CodexAuthCacheError(msg)
        return raw

    def write(self, raw: bytes) -> None:
        account = login_account(raw, EXECUTOR_LOGIN_ENTRY)
        result = self.run(["sh", "-c", WRITE_AUTH_SCRIPT, "_", self.home], stdin=raw.decode())
        if result.returncode != 0:
            msg = f"the executor auth file could not be written (exit {result.returncode})"
            raise CodexAuthCacheError(msg)
        self.account = account


def _last_refresh(raw: bytes) -> str:
    try:
        value = json.loads(raw).get("last_refresh")
    except (ValueError, AttributeError):
        return ""
    return value if isinstance(value, str) else ""


class ExecutorAuthCache(CodexAuthCache):
    """Under the lease, end whatever a dead server left running and recover a newer login of the same account."""

    def __init__(self, auth_file: ExecutorAuthFile) -> None:
        super().__init__(executor_lease_dir(), entry=EXECUTOR_LOGIN_ENTRY, store=auth_file)
        self.auth_file = auth_file

    def hydrate(self) -> Path:
        end_codex_processes(self.auth_file.docker, self.auth_file.container_id)
        account = login_account(self.stored(), EXECUTOR_LOGIN_ENTRY)
        for home in self._stale_homes():
            stale = ExecutorAuthFile(self.auth_file.docker, self.auth_file.container_id, home, account=account)
            try:
                raw = stale.read()
            except CodexAuthCacheError:
                raw = b""
            if raw and _last_refresh(raw) > _last_refresh(self.stored()):
                self.write_verified(encode_auth_cache(raw))
            stale.run(["rm", "-rf", "--", home])
        return super().hydrate()

    def _stale_homes(self) -> list[str]:
        listing = self.auth_file.run(["sh", "-c", LIST_HOMES_SCRIPT])
        if listing.returncode != 0:
            msg = f"the executor homes could not be listed (exit {listing.returncode})"
            raise CodexAuthCacheError(msg)
        return [home for home in listing.stdout.split() if home != self.auth_file.home]


class ExecutorCodexAppServer(SharedCodexAppServer):
    """One App Server lifetime in one executor container, from a home no earlier server used."""

    def __init__(self, attestation: ExecutorAttestation, *, docker: ExecutorDocker | None = None) -> None:
        self.attestation = attestation
        self.docker = docker or ExecutorDocker()
        self.home = f"{EXECUTOR_HOME_ROOT}/codex-{secrets.token_hex(8)}"
        self.auth_file = ExecutorAuthFile(self.docker, attestation.container_id, self.home)
        super().__init__(
            code_home=executor_lease_dir(),
            cache=ExecutorAuthCache(self.auth_file),
            command=self.docker.app_server_argv(attestation.container_id, self.home),
            process_env={"PATH": os.environ.get("PATH", "")},
        )

    def attestation_refusal(self, executor: ExecutorAttestation | None) -> CodexAppServerError | None:
        if executor is not None and executor.container_id == self.attestation.container_id:
            return None
        return CodexAppServerError.wrong_transport(f"options not attested for executor {self.attestation.container_id}")

    def successor(self, options: CodexAppServerOptions) -> SharedCodexAppServer:
        return executor_codex_app_server(options.executor or self.attestation)

    async def _serve(self, options: CodexAppServerOptions) -> None:
        try:
            await super()._serve(options)
        finally:
            await asyncio.to_thread(end_codex_processes, self.docker, self.attestation.container_id)
        await asyncio.to_thread(self.auth_file.run, ["rm", "-rf", "--", self.home])

    async def _open_thread(
        self, options: CodexAppServerOptions, resume: str | None, refused_threads: list[str]
    ) -> tuple[str, str]:
        phase = self.attestation.phase if options.executor is None else options.executor.phase
        try:
            renewed = await asyncio.to_thread(
                attest, phase, options.runtime_workspace_roots, home=self.home, docker=self.docker
            )
        except PlantedHomeError as exc:
            self._retiring.set()
            if self._stop is not None:
                self._stop.set()
            raise _refused(phase, exc) from exc
        except ExecutorRefusalError as exc:
            raise _refused(phase, exc) from exc
        if renewed.container_id != self.attestation.container_id:
            raise _refused(phase, f"the executor was replaced by {renewed.container_id}")
        return await super()._open_thread(options, resume, refused_threads)


def _refused(phase: str, detail: object) -> HarnessFallbackError:
    return HarnessFallbackError(
        refusal_reason(phase, detail), kind=HarnessFallbackKind.ACCESS, side_effects_started=False
    )


_managers: dict[tuple[int, str, str], ExecutorCodexAppServer] = {}
_managers_lock = threading.Lock()


def executor_codex_app_server(attestation: ExecutorAttestation) -> ExecutorCodexAppServer:
    key = (os.getpid(), "ro-executor", attestation.container_id)
    with _managers_lock:
        current = _managers.get(key)
        if current is not None and current.failed:
            current.close()
            if not current.stopped:
                msg = "Previous executor Codex App Server has not stopped"
                raise CodexAppServerError(msg)
            del _managers[key]
        if key not in _managers:
            _managers[key] = ExecutorCodexAppServer(attestation)
        return _managers[key]


def reset_executor_app_servers() -> None:
    with _managers_lock:
        for manager in _managers.values():
            manager.close()
            if not manager.stopped:
                msg = "Executor Codex App Server has not stopped"
                raise CodexAppServerError(msg)
        _managers.clear()


atexit.register(reset_executor_app_servers)


@asynccontextmanager
async def executor_session(options: ClaudeAgentOptions, phase: str) -> AsyncIterator[SharedCodexSession]:
    """Attest live, then open a thread on the executor's server; a refusal starts nothing."""
    try:
        attestation = await asyncio.to_thread(attest, phase, workspace_roots(HarnessOptions.from_sdk_options(options)))
    except ExecutorRefusalError as exc:
        raise _refused(phase, exc) from exc
    translated = CodexAppServerOptions.from_sdk_options(options, executor=attestation)
    session = SharedCodexSession(translated, manager=executor_codex_app_server(attestation), resume=None)
    try:
        async with opened_session(session):
            yield session
    except ExecutorRefusalError as exc:
        raise _refused(phase, exc) from exc
    except CodexAuthCacheError as exc:
        raise _login_unusable(session, exc) from exc


def _login_unusable(session: SharedCodexSession, exc: CodexAuthCacheError) -> HarnessFallbackError:
    return HarnessFallbackError(
        f"the read-only executor's Codex login could not be used or kept ({exc}); re-import pass entry "
        f"{EXECUTOR_LOGIN_ENTRY!r} with `t3 codex auth import --executor --from -` in teatree-worker",
        kind=HarnessFallbackKind.AUTH,
        side_effects_started=bool(session.turn_id),
        agent_session_id=session.thread_id,
    )
