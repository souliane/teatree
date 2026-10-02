"""Codex App Server harness for managed ChatGPT headless work."""

import asyncio
import json
import shutil
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock

from teatree.agents.codex_app_server_env import codex_command, codex_process_env
from teatree.agents.codex_app_server_errors import (
    auth_persist_error,
    managed_auth_error,
    request_error,
    transport_error,
    turn_error,
)
from teatree.agents.codex_app_server_messages import tool_blocks, translate_usage
from teatree.agents.codex_app_server_options import (
    CodexAppServerError,
    CodexAppServerOptions,
    codex_phase_policy_unavailable_reason,
)
from teatree.agents.codex_auth_cache import CodexAuthCache, CodexAuthCacheError, resolve_codex_home
from teatree.agents.harness_registry import HarnessCapabilities, HarnessFallbackError, HarnessSpec
from teatree.agents.sdk_tool_map import sdk_disallowed_tools_for_phase

if TYPE_CHECKING:
    from claude_agent_sdk.types import ModelUsage

    from teatree.agents.harness import HarnessSession
    from teatree.agents.harness_registry import HarnessBuildContext

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


@dataclass(frozen=True, slots=True)
class _StreamFailure:
    error: CodexAppServerError | HarnessFallbackError


class CodexAppServerSession:
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
        self._closing = False
        self._text_by_item: dict[str, list[str]] = {}
        self._emitted_items: set[str] = set()
        self._text: list[str] = []
        self._usage: dict[str, Any] | None = None
        self._side_effects_started = False

    async def start(self, *, open_thread: bool = True) -> None:
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
        if not _chatgpt_authenticated(account):
            raise managed_auth_error()
        if not open_thread:
            return
        method = "thread/resume" if self.resume else "thread/start"
        params = _thread_params(self.options)
        if self.resume:
            params = {"threadId": self.resume, **params}
        response = await self._request(method, params)
        thread = response.get("thread")
        self.thread_id = str(thread.get("id", "")) if isinstance(thread, dict) else ""
        self.model = str(response.get("model") or self.model)
        if not self.thread_id:
            raise CodexAppServerError.missing_thread_id()

    async def query(self, prompt: str) -> None:
        # The previous completed turn is no longer evidence that this request has
        # been accepted. Only the fresh turn/start response may make a subsequent
        # transport failure replay-ambiguous.
        self.turn_id = ""
        self._text_by_item.clear()
        self._emitted_items.clear()
        self._text.clear()
        self._usage = None
        self._side_effects_started = False
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

    async def receive_response(self) -> AsyncIterator[object]:
        while True:
            event = await self._next_event()
            messages, completed = self._translate_event(event)
            for message in messages:
                yield message
            if completed:
                return

    def _translate_event(self, event: Mapping[str, Any]) -> tuple[list[object], bool]:
        method = event.get("method")
        params = event.get("params")
        if not isinstance(params, dict) or params.get("threadId") != self.thread_id:
            return [], False
        if method == "item/agentMessage/delta":
            self._record_delta(params)
        elif method == "item/started":
            self._record_possible_side_effect(params.get("item"))
        elif method == "item/completed":
            self._record_possible_side_effect(params.get("item"))
            message = self._completed_item_message(params.get("item"))
            return ([message] if message is not None else []), False
        elif method == "thread/tokenUsage/updated":
            self._usage = translate_usage(params.get("tokenUsage"))
        elif method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict):
                raise CodexAppServerError.missing_turn()
            if turn.get("error") is not None:
                raise turn_error(
                    turn.get("error"),
                    side_effects_started=self._side_effects_started,
                    agent_session_id=self.thread_id,
                )
            return [*self._remaining_turn_messages(turn), self._result_message(turn)], True
        return [], False

    async def interrupt(self) -> None:
        if self.thread_id and self.turn_id:
            await self._request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id})

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        process = self.process
        if process is None:
            return
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await process.stdin.wait_closed()
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in (self._stdout_task, self._stderr_task):
            if task is not None:
                with suppress(asyncio.CancelledError, CodexAppServerError):
                    await task
        self._fail_pending(CodexAppServerError.stopped())

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
            while line := await process.stdout.readline():
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

    def _route_stdout_line(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CodexAppServerError.invalid_protocol() from exc
        if not isinstance(message, dict):
            raise CodexAppServerError.invalid_protocol()
        if "method" in message and "id" in message:
            raise CodexAppServerError.invalid_protocol()
        if "method" in message:
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

    async def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        while await process.stderr.read(65536):
            pass

    async def _next_event(self) -> dict[str, Any]:
        event = await self._events.get()
        if isinstance(event, _StreamFailure):
            raise event.error
        return event

    async def next_transport_event(self) -> dict[str, Any] | _StreamFailure:
        """Read one raw event for a worker-owned multi-thread dispatcher."""
        return await self._events.get()

    async def request_protocol(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Issue an App Server request from a worker-owned multi-thread dispatcher."""
        return await self._request(method, params)

    def _fail_pending(self, error: CodexAppServerError | HarnessFallbackError) -> None:
        for _method, future in self._pending.values():
            if not future.done():
                future.set_exception(error)

    def _record_delta(self, params: Mapping[str, Any]) -> None:
        item_id = params.get("itemId")
        delta = params.get("delta")
        if isinstance(item_id, str) and isinstance(delta, str):
            self._text_by_item.setdefault(item_id, []).append(delta)

    def _record_possible_side_effect(self, value: object) -> None:
        if isinstance(value, dict) and value.get("type") in {
            "collabAgentToolCall",
            "commandExecution",
            "fileChange",
            "mcpToolCall",
            "subAgentActivity",
            "dynamicToolCall",
            "imageGeneration",
        }:
            self._side_effects_started = True

    def _completed_item_message(self, value: object) -> AssistantMessage | None:
        if not isinstance(value, dict):
            return None
        item_id = str(value.get("id", ""))
        if not item_id or item_id in self._emitted_items:
            return None
        self._emitted_items.add(item_id)
        item_type = value.get("type")
        if item_type == "agentMessage":
            text = str(value.get("text") or "".join(self._text_by_item.get(item_id, ())))
            if not text:
                return None
            self._text.append(text)
            return AssistantMessage(content=[TextBlock(text=text)], model=self.model)
        blocks = tool_blocks(value)
        return AssistantMessage(content=blocks, model=self.model) if blocks else None

    def _remaining_turn_messages(self, turn: Mapping[str, Any]) -> list[AssistantMessage]:
        messages: list[AssistantMessage] = []
        items = turn.get("items")
        if isinstance(items, list):
            for item in items:
                message = self._completed_item_message(item)
                if message is not None:
                    messages.append(message)
        for item_id, chunks in self._text_by_item.items():
            if item_id not in self._emitted_items and chunks:
                text = "".join(chunks)
                self._emitted_items.add(item_id)
                self._text.append(text)
                messages.append(AssistantMessage(content=[TextBlock(text=text)], model=self.model))
        return messages

    def _result_message(self, turn: Mapping[str, Any]) -> ResultMessage:
        status = str(turn.get("status", ""))
        is_error = status != "completed"
        error = turn.get("error")
        detail = error.get("message") if isinstance(error, dict) else None
        duration = turn.get("durationMs")
        duration_ms = int(duration) if isinstance(duration, int | float) else 0
        model_usage = cast("dict[str, ModelUsage]", {self.model: {}})
        return ResultMessage(
            subtype="success" if not is_error else "error_during_execution",
            duration_ms=duration_ms,
            duration_api_ms=duration_ms,
            is_error=is_error,
            num_turns=1,
            session_id=self.thread_id,
            total_cost_usd=None,
            usage=self._usage,
            result=str(detail) if detail else "".join(self._text),
            model_usage=model_usage,
        )


class CodexAppServerHarness:
    capabilities = CODEX_APP_SERVER_CAPABILITIES

    def __init__(self, *, code_home: Path | None = None, command: Sequence[str] | None = None) -> None:
        self.code_home = resolve_codex_home(code_home)
        self.command = command

    @asynccontextmanager
    async def open(self, options: ClaudeAgentOptions) -> AsyncIterator["HarnessSession"]:
        translated = CodexAppServerOptions.from_sdk_options(options)
        if self.command is None:
            from teatree.agents.codex_shared_app_server import (  # noqa: PLC0415 — late import avoids a transport cycle
                SharedCodexSession,
                shared_codex_app_server,
            )

            session = SharedCodexSession(
                translated,
                manager=shared_codex_app_server(self.code_home),
                resume=options.resume,
            )
            try:
                await session.start()
                try:
                    yield session
                finally:
                    await session.close()
            except CodexAuthCacheError as exc:
                if session.thread_id:
                    raise auth_persist_error(
                        side_effects_started=bool(session.turn_id),
                        agent_session_id=session.thread_id,
                    ) from exc
                raise managed_auth_error() from exc
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
                try:
                    await session.start()
                    yield session
                finally:
                    await session.close()
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
        factory=lambda _context: CodexAppServerHarness(),
        capabilities=CODEX_APP_SERVER_CAPABILITIES,
        allows_provider=False,
        unavailable_reason=_codex_unavailable_reason,
    )


def _codex_unavailable_reason(context: "HarnessBuildContext") -> str | None:
    if shutil.which("codex") is None:
        return "codex CLI is not installed or not on PATH"
    if context.phase and (
        reason := codex_phase_policy_unavailable_reason(sdk_disallowed_tools_for_phase(context.phase))
    ):
        return f"phase {context.phase!r} {reason.lower()}"
    return None


def _thread_params(options: CodexAppServerOptions) -> dict[str, Any]:
    params: dict[str, Any] = {
        "cwd": options.core.cwd,
        "developerInstructions": options.core.system_prompt,
        "approvalPolicy": "never",
        "sandbox": options.sandbox_mode,
        "runtimeWorkspaceRoots": list(options.runtime_workspace_roots),
    }
    if options.core.model is not None:
        params["model"] = options.core.model
    if options.config:
        params["config"] = options.config
    return params


def _chatgpt_authenticated(account: Mapping[str, Any]) -> bool:
    payload = account.get("account")
    return isinstance(payload, dict) and payload.get("type") == "chatgpt"
