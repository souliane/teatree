"""Codex App Server harness for managed ChatGPT headless work."""

import asyncio
import json
import logging
import shutil
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents import codex_app_server_events
from teatree.agents.codex_app_server_env import codex_command, codex_process_env
from teatree.agents.codex_app_server_errors import (
    METHOD_NOT_FOUND,
    auth_persist_error,
    managed_auth_error,
    request_error,
    transport_error,
)
from teatree.agents.codex_app_server_events import CodexToolEvents
from teatree.agents.codex_app_server_events import thread_params as _thread_params
from teatree.agents.codex_app_server_lifecycle import chatgpt_authenticated, opened_session, opened_shared_session
from teatree.agents.codex_app_server_messages import CodexEventTranslator
from teatree.agents.codex_app_server_options import (
    CodexAppServerError,
    CodexAppServerOptions,
    codex_container_unavailable_reason,
    codex_model_unavailable_reason,
    codex_worker_denials,
    codex_worker_unavailable_reason,
    container_is_the_sandbox,
)
from teatree.agents.codex_approval_gate import APPROVAL_METHODS, approval_decision
from teatree.agents.codex_auth_cache import CodexAuthCache, CodexAuthCacheError, resolve_codex_home
from teatree.agents.codex_mcp_probe import refuse_unjudged_mcp_servers
from teatree.agents.codex_read_only_executor import (
    EXECUTOR_PHASES,
    executor_availability_reason,
    read_only_executor_refusal,
)
from teatree.agents.codex_turn_steering import CodexTurnSteering
from teatree.agents.harness_registry import HarnessCapabilities, HarnessFallbackError, HarnessFallbackKind, HarnessSpec
from teatree.agents.sdk_tool_map import CODEX_WORKER_EXECUTION_PHASES, phase_bars_write_tools

if TYPE_CHECKING:
    from teatree.agents.harness import HarnessSession
    from teatree.agents.harness_registry import HarnessBuildContext

logger = logging.getLogger(__name__)

TERMINATE_SECONDS = 5.0
# A ChatGPT login's mcpServerStatus/list reply measured ~930 KB on one line; asyncio's default is 64 KiB.
PROTOCOL_LINE_LIMIT = 16 * 1024 * 1024
_STDERR_TAIL_BYTES = 2048


def transport_close_seconds() -> float:
    """The longest close() can take: the report drain, then terminate, then kill."""
    return codex_app_server_events.POST_SETTLE_SECONDS + 2 * TERMINATE_SECONDS


CODEX_APP_SERVER_CAPABILITIES = HarnessCapabilities(
    hooks=False,
    mcp=True,
    cache_control=False,
    server_resume=True,
    structured_output=False,
    spawns_cli_child=False,
    metered_lane=False,
    managed_lane=True,
)


def codex_capabilities() -> HarnessCapabilities:
    return replace(CODEX_APP_SERVER_CAPABILITIES, mcp=not container_is_the_sandbox())


@dataclass(frozen=True, slots=True)
class _StreamFailure:
    error: CodexAppServerError | HarnessFallbackError


class CodexAppServerSession(CodexTurnSteering):
    def __init__(
        self,
        options: CodexAppServerOptions,
        *,
        resume: str | None,
        code_home: Path,
        command: Sequence[str] | None = None,
        process_env: Mapping[str, str] | None = None,
    ) -> None:
        self.options = options
        self.resume = resume
        self.code_home = code_home
        self.command = tuple(command) if command is not None else None
        self.process_env = dict(process_env) if process_env is not None else None
        self.process: asyncio.subprocess.Process | None = None
        self.thread_id = ""
        self.turn_id = ""
        self.model = options.core.model or "codex"
        self._next_id = 0
        self._pending: dict[int, tuple[str, asyncio.Future[dict[str, Any]]]] = {}
        self._events: asyncio.Queue[dict[str, Any] | _StreamFailure] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stream_failure: CodexAppServerError | HarnessFallbackError | None = None
        self._stderr_tail = b""
        self._closing = False
        self.translator = CodexEventTranslator()
        self._tool_events = CodexToolEvents()
        self._server_request_tasks: set[asyncio.Task[None]] = set()

    async def start(self, *, open_thread: bool = True) -> None:
        if self.options.executor is not None and self.options.executor.container_id not in (self.command or ()):
            raise CodexAppServerError.attested_on_local_transport(self.code_home)
        command = self.command or codex_command()
        env = dict(self.process_env) if self.process_env is not None else codex_process_env(self.code_home)
        env.update({"CODEX_HOME": str(self.code_home), "HOME": str(self.code_home)})
        try:
            self.process = await asyncio.create_subprocess_exec(
                *command,
                "app-server",
                "--listen",
                "stdio://",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=PROTOCOL_LINE_LIMIT,
            )
            self._stdout_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._drain_stderr())
            await self._initialize_protocol(open_thread=open_thread)
        except OSError as exc:
            await self.close()
            error = transport_error("process startup")
            raise error from exc
        except BaseException:
            await self.close()
            raise

    async def _initialize_protocol(self, *, open_thread: bool = True) -> None:
        # Without experimentalApi the server refuses runtimeWorkspaceRoots on thread and turn requests (-32600).
        await self._request(
            "initialize",
            {"clientInfo": {"name": "teatree", "version": "0.0.1"}, "capabilities": {"experimentalApi": True}},
        )
        await self._notify("initialized", {})
        account = await self._request("account/read", {"refreshToken": False})
        if not chatgpt_authenticated(account):
            raise managed_auth_error()
        if not open_thread:
            return
        method = "thread/resume" if self.resume else "thread/start"
        params = _thread_params(self.options, resuming=bool(self.resume))
        if self.resume:
            params = {"threadId": self.resume, **params}
        response = await self._request(method, params)
        thread = response.get("thread")
        self.thread_id = str(thread.get("id", "")) if isinstance(thread, dict) else ""
        self.model = str(response.get("model") or self.model)
        if not self.thread_id:
            raise CodexAppServerError.missing_thread_id()
        await refuse_unjudged_mcp_servers(self._request, self.thread_id, self.options)
        self.register_thread(self.thread_id, self.options)

    def register_thread(self, thread_id: str, options: CodexAppServerOptions) -> None:
        self._tool_events.register(thread_id, options)

    def unregister_thread(self, thread_id: str) -> None:
        self._tool_events.unregister(thread_id)

    async def query(self, prompt: str) -> None:
        # The previous completed turn is no longer evidence that this request has
        # been accepted. Only the fresh turn/start response may make a subsequent
        # transport failure replay-ambiguous.
        self.turn_id = ""
        self.translator.reset()
        params: dict[str, Any] = {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": prompt}],
            "cwd": self.options.core.cwd,
            "effort": self.options.core.effort,
            "sandboxPolicy": self.options.sandbox_policy,
            "runtimeWorkspaceRoots": list(self.options.runtime_workspace_roots),
        }
        if self.options.core.model is not None:
            params["model"] = self.options.core.model
        response = await self._request("turn/start", params)
        turn = response.get("turn")
        self.turn_id = str(turn.get("id", "")) if isinstance(turn, dict) else ""
        if not self.turn_id:
            raise CodexAppServerError.missing_turn()
        self._turn_opened(self.turn_id)

    async def receive_response(self) -> AsyncIterator[object]:
        try:
            while True:
                event = await self._next_event()
                messages, completed = self.translator.translate(event, thread_id=self.thread_id, model=self.model)
                for message in messages:
                    yield message
                if completed:
                    return
        finally:
            self._turn_closed()

    async def interrupt(self) -> None:
        if self.thread_id and self.turn_id:
            await self._request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id})

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._turn_closed(session=True)
        process = self.process
        if process is None:
            return
        await self._tool_events.drain_post_tasks()
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await process.stdin.wait_closed()
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=TERMINATE_SECONDS)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in (self._stdout_task, self._stderr_task):
            if task is not None:
                with suppress(asyncio.CancelledError, CodexAppServerError):
                    await task
        self._log_stream_failure()
        for task in tuple(self._server_request_tasks):
            task.cancel()
        for task in tuple(self._server_request_tasks):
            with suppress(asyncio.CancelledError):
                await task
        self._fail_pending(CodexAppServerError.stopped())

    def _log_stream_failure(self) -> None:
        if self._stream_failure is not None:
            stderr_tail = self._stderr_tail.decode(errors="replace")
            logger.warning("Codex App Server stream failed: %s; stderr tail: %r", self._stream_failure, stderr_tail)

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._stream_failure is not None:
            raise self._stream_failure
        process = self.process
        if process is None or process.stdin is None:
            raise CodexAppServerError.not_running()
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (method, future)
        try:
            await self._write({"id": request_id, "method": method, "params": params})
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        message: dict[str, Any] = {"method": method}
        if params:
            message["params"] = params
        await self._write(message)

    async def _write(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.stdin.is_closing():
            raise CodexAppServerError.not_running()
        encoded = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:
            process.stdin.write(encoded)
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                error = transport_error(
                    "request write",
                    side_effects_started=bool(self.turn_id),
                    agent_session_id=self.thread_id,
                )
                raise error from exc

    async def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        failure: CodexAppServerError | HarnessFallbackError = CodexAppServerError.stopped()
        try:
            while line := await self._read_protocol_line(process.stdout):
                self._route_stdout_line(line)
            if not self._closing:
                failure = transport_error(
                    "protocol stream",
                    side_effects_started=bool(self.turn_id),
                    agent_session_id=self.thread_id,
                )
        except CodexAppServerError as exc:
            failure = exc
        finally:
            if not self._closing:
                self._stream_failure = failure
                self._fail_pending(failure)
                self._events.put_nowait(_StreamFailure(failure))

    @staticmethod
    async def _read_protocol_line(stdout: asyncio.StreamReader) -> bytes:
        try:
            return await stdout.readline()
        except ValueError as exc:
            # StreamReader.readline re-raises LimitOverrunError as a bare ValueError.
            raise CodexAppServerError.oversize_line(PROTOCOL_LINE_LIMIT) from exc

    def _route_stdout_line(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CodexAppServerError.invalid_protocol() from exc
        if not isinstance(message, dict):
            raise CodexAppServerError.invalid_protocol()
        if "method" in message and "id" in message:
            self._route_server_request(message)
            return
        if "method" in message:
            self._tool_events.observe(message)
            self._events.put_nowait(message)
            return
        request_id = message.get("id")
        pending = self._pending.get(request_id) if isinstance(request_id, int) else None
        if pending is None:
            return
        method, future = pending
        if future.done():
            return
        if "error" in message:
            future.set_exception(
                request_error(
                    method,
                    message.get("error"),
                    side_effects_started=bool(self.turn_id),
                    agent_session_id=self.thread_id,
                )
            )
            return
        result = message.get("result")
        future.set_result(result if isinstance(result, dict) else {})

    def _route_server_request(self, message: dict[str, Any]) -> None:
        method = message["method"]
        is_approval = isinstance(method, str) and method in APPROVAL_METHODS
        handler = (
            self._respond_approval
            if is_approval
            else self._respond_mailbox
            if method == "item/tool/call"
            else self._refuse_server_request
        )
        task = asyncio.create_task(handler(message))
        self._server_request_tasks.add(task)
        task.add_done_callback(self._server_request_done)

    def _server_request_done(self, task: asyncio.Task[None]) -> None:
        self._server_request_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning("Codex server request could not be answered: %s", type(error).__name__)

    async def _refuse_server_request(self, message: dict[str, Any]) -> None:
        if not self._closing:
            await self._write(
                {"id": message["id"], "error": {"code": METHOD_NOT_FOUND, "message": "Unsupported server request."}}
            )

    async def _respond_mailbox(self, message: dict[str, Any]) -> None:
        result = await codex_app_server_events.mailbox_tool_result(
            message.get("params"), self._tool_events.approval_options
        )
        if result is None:
            await self._refuse_server_request(message)
            return
        if not self._closing:
            await self._write({"id": message["id"], "result": result})

    async def _respond_approval(self, message: dict[str, Any]) -> None:
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        method = message["method"]
        raw_thread_id = params.get("threadId")
        thread_id = raw_thread_id if isinstance(raw_thread_id, str) else ""
        options = self._tool_events.approval_options.get(thread_id)
        changes = self._tool_events.file_changes.pop((thread_id, str(params.get("itemId", ""))), None)
        decision, reason = "decline", "TeaTree PreToolUse router could not evaluate the action."
        try:
            if options is not None:
                await self._tool_events.wait_for_post(thread_id)
            decision, reason = await approval_decision(method, params, options, changes)
        finally:
            if not self._closing:
                try:
                    turn_id = params.get("turnId")
                    if options is not None and reason and isinstance(turn_id, str):
                        await self._steer_refusal(thread_id, turn_id, reason)
                finally:
                    await self._write({"id": message["id"], "result": {"decision": decision}})

    async def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        while chunk := await process.stderr.read(65536):
            self._stderr_tail = (self._stderr_tail + chunk)[-_STDERR_TAIL_BYTES:]

    async def _next_event(self) -> dict[str, Any]:
        event = await self.next_transport_event()
        if isinstance(event, _StreamFailure):
            raise event.error
        await self.settle_turn(event)
        return event

    async def next_transport_event(self) -> dict[str, Any] | _StreamFailure:
        """Read one raw event for a worker-owned multi-thread dispatcher; it never waits on a thread's reports."""
        return await self._events.get()

    async def settle_turn(self, event: Mapping[str, Any]) -> None:
        """Let a completed turn's PostToolUse reports land before its own thread reads the completion."""
        params = event.get("params")
        thread_id = params.get("threadId") if isinstance(params, dict) else None
        if event.get("method") == "turn/completed" and isinstance(thread_id, str):
            await self._tool_events.wait_for_post(thread_id)

    async def request_protocol(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Issue an App Server request from a worker-owned multi-thread dispatcher."""
        return await self._request(method, params)

    def _fail_pending(self, error: CodexAppServerError | HarnessFallbackError) -> None:
        for _method, future in self._pending.values():
            if not future.done():
                future.set_exception(error)


class CodexAppServerHarness:
    def __init__(
        self,
        *,
        refusal: str | None,
        code_home: Path | None = None,
        command: Sequence[str] | None = None,
        read_only_phase: str | None = None,
        worker_execution_phase: str | None = None,
    ) -> None:
        self.capabilities = codex_capabilities()
        if read_only_phase is not None:
            self.capabilities = replace(self.capabilities, read_only_venue=True)
        self.code_home = resolve_codex_home(code_home)
        self.command = command
        self.refusal = refusal
        self.read_only_phase = read_only_phase
        self.worker_execution_phase = worker_execution_phase

    @asynccontextmanager
    async def open(self, options: ClaudeAgentOptions) -> AsyncGenerator["HarnessSession"]:
        if self.refusal:
            raise HarnessFallbackError(self.refusal, kind=HarnessFallbackKind.ACCESS, side_effects_started=False)
        from teatree.agents import (  # noqa: PLC0415 — late import avoids a transport cycle
            codex_read_only_server,
            codex_shared_app_server,
        )

        if self.read_only_phase is not None:
            async with codex_read_only_server.executor_session(options, self.read_only_phase) as executor_session:
                yield executor_session
            return
        if self.worker_execution_phase is not None:
            options = replace(
                options,
                disallowed_tools=list(codex_worker_denials(options.disallowed_tools, self.worker_execution_phase)),
            )
        translated = CodexAppServerOptions.from_sdk_options(options)
        if self.command is None:
            session = codex_shared_app_server.SharedCodexSession(
                translated,
                manager=codex_shared_app_server.shared_codex_app_server(self.code_home, translated.core.env),
                resume=options.resume,
            )
            async with opened_shared_session(session):
                yield session
            return
        cache = CodexAuthCache(self.code_home)
        cache_hydrated = False
        session: CodexAppServerSession | None = None
        try:
            async with cache.session():
                cache_hydrated = True
                session = CodexAppServerSession(
                    translated,
                    resume=options.resume,
                    code_home=self.code_home,
                    command=self.command,
                    process_env=translated.core.env if self.command is not None else None,
                )
                async with opened_session(session):
                    yield session
        except CodexAuthCacheError as exc:
            if cache_hydrated and session is not None:
                raise auth_persist_error(
                    side_effects_started=bool(session.turn_id),
                    agent_session_id=session.thread_id,
                ) from exc
            raise managed_auth_error() from exc


def codex_app_server_spec() -> HarnessSpec:
    return HarnessSpec(
        name="codex_app_server",
        factory=lambda context: CodexAppServerHarness(
            refusal=_codex_unavailable_reason(context),
            read_only_phase=_read_only_phase(context),
            worker_execution_phase=(
                context.phase if context.phase in CODEX_WORKER_EXECUTION_PHASES and container_is_the_sandbox() else None
            ),
        ),
        capabilities=codex_capabilities(),
        allows_provider=False,
        unavailable_reason=_codex_unavailable_reason,
        read_only_executor_refusal=read_only_executor_refusal,
    )


def _read_only_phase(context: "HarnessBuildContext") -> str | None:
    if (
        context.phase
        and context.phase not in CODEX_WORKER_EXECUTION_PHASES
        and container_is_the_sandbox()
        and phase_bars_write_tools(context.phase)
    ):
        return context.phase
    return None


def _codex_unavailable_reason(context: "HarnessBuildContext") -> str | None:
    if shutil.which("codex") is None:
        return "codex CLI is not installed or not on PATH"
    if reason := codex_container_unavailable_reason():
        return reason
    if context.phase in EXECUTOR_PHASES and container_is_the_sandbox():
        return executor_availability_reason(context.phase) or codex_model_unavailable_reason(
            resolve_codex_home(), context.model
        )
    return codex_worker_unavailable_reason(context)
